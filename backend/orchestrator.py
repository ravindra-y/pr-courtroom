import asyncio
import difflib
import time
import uuid
from pathlib import Path
from typing import Any, AsyncIterator, Optional

from backend.agents.defender import DefenderAgentError, run_defender
from backend.agents.judge import JudgeAgentError, run_judge
from backend.agents.prosecutor import ProsecutorAgentError, run_prosecutor
from backend.github_client import fetch_pr_and_prepare_workdir, read_relevant_head_files
from backend.models import Charge, DefenseResponse, JudgeOutput, RebuttalRuling, TestResult
from backend.replay import clamp_delay, format_sse, save_fixture, stream_replay_events
from backend.sandbox import (
    classify_charges_parallel,
    run_charge_test_after_fix,
    run_existing_tests,
)
from backend.scoring import compute_verdict


def combine_file_fixes(original: str, fixed_versions: list[str]) -> str:
    """
    Combine one or more replacement file versions against `original`.
    If multiple distinct versions are provided, merge non-overlapping line replacements.
    """
    unique: list[str] = []
    for v in fixed_versions:
        if v and v not in unique:
            unique.append(v)
    if not unique:
        return original
    if len(unique) == 1:
        return unique[0]

    orig_lines = original.splitlines(keepends=True)
    # Collect line-range replacements (start, end, new_lines) from each version
    edits: list[tuple[int, int, list[str]]] = []
    for ver in unique:
        ver_lines = ver.splitlines(keepends=True)
        matcher = difflib.SequenceMatcher(None, orig_lines, ver_lines)
        for tag, i1, i2, j1, j2 in matcher.get_opcodes():
            if tag != "equal":
                edits.append((i1, i2, ver_lines[j1:j2]))

    # Sort edits by start index; if overlapping, fall back to the last version
    edits.sort(key=lambda e: (e[0], e[1]))
    deduped: list[tuple[int, int, list[str]]] = []
    for edit in edits:
        if deduped and deduped[-1][:2] == edit[:2] and deduped[-1][2] == edit[2]:
            continue
        if deduped and edit[0] < deduped[-1][1]:
            # Overlapping edits: assume the final version already combined changes
            return unique[-1]
        deduped.append(edit)

    result: list[str] = []
    cursor = 0
    for i1, i2, replacement in deduped:
        result.extend(orig_lines[cursor:i1])
        result.extend(replacement)
        cursor = i2
    result.extend(orig_lines[cursor:])
    return "".join(result)


def compute_unified_diff(file_path: str, old_text: str, new_text: str) -> str:
    return "".join(
        difflib.unified_diff(
            old_text.splitlines(keepends=True),
            new_text.splitlines(keepends=True),
            fromfile=f"a/{file_path}",
            tofile=f"b/{file_path}",
        )
    )


def _drop_reason_for_status(status: str) -> Optional[str]:
    if status == "pass":
        return "overruled by evidence"
    if status in ("error", "timeout"):
        return "broken test"
    if status == "fail_preexisting":
        return "pre-existing"
    return None


async def run_trial_stream(
    pr_url: str,
    replay: bool = False,
    workdir_root: Optional[Path] = None,
) -> AsyncIterator[str]:
    """
    Orchestrate a full PR Courtroom trial and yield SSE events.
    When replay=True, streams from backend/fixtures/ without needing an API key.
    """
    if replay:
        try:
            async for frame in stream_replay_events(pr_url=pr_url):
                yield frame
        except Exception as exc:
            yield format_sse("error", {"message": f"Replay error: {exc}"})
        return

    start_time = time.monotonic()
    last_emit_time = start_time
    recorded_events: list[dict[str, Any]] = []

    def record_and_format(event: str, data: dict[str, Any]) -> str:
        nonlocal last_emit_time
        now = time.monotonic()
        delay = clamp_delay(now - last_emit_time)
        last_emit_time = now
        recorded_events.append({"event": event, "data": data, "delay": delay})
        return format_sse(event, data)

    run_id = uuid.uuid4().hex[:10]

    try:
        # Step 1: Fetch PR & download base/head zips into workdir/<run_id>/{base,head}
        pr_ctx = await fetch_pr_and_prepare_workdir(
            pr_url=pr_url,
            run_id=run_id,
            workdir_root=workdir_root,
        )
        yield record_and_format(
            "trial_started",
            {
                "pr_title": pr_ctx.pr_title,
                "files_changed": pr_ctx.files_changed,
                "pr_url": pr_ctx.pr_url,
            },
        )

        head_files = read_relevant_head_files(pr_ctx.head_dir, pr_ctx.files_changed)

        # Step 2: Prosecutor files up to 3 charges
        prosecutor_out = await run_prosecutor(
            pr_title=pr_ctx.pr_title,
            pr_description=pr_ctx.pr_description,
            diff=pr_ctx.diff,
            head_files=head_files,
        )
        charges = prosecutor_out.charges[:3]
        for charge in charges:
            yield record_and_format("charge_filed", charge.model_dump())

        # Step 3: Evidence check in parallel (asyncio.gather)
        initial_results = await classify_charges_parallel(
            charges=charges,
            base_dir=pr_ctx.base_dir,
            head_dir=pr_ctx.head_dir,
        )

        valid_charges: list[Charge] = []
        valid_test_results: dict[str, TestResult] = {}
        charges_by_id = {c.id: c for c in charges}
        charges_dropped_count = 0

        for res in initial_results:
            yield record_and_format("test_result", res.model_dump())
            drop_reason = _drop_reason_for_status(res.status)
            if drop_reason is not None:
                charges_dropped_count += 1
                yield record_and_format(
                    "charge_dropped",
                    {"charge_id": res.charge_id, "reason": drop_reason},
                )
            elif res.status == "fail_valid" and res.charge_id in charges_by_id:
                valid_charges.append(charges_by_id[res.charge_id])
                valid_test_results[res.charge_id] = res

        # Step 4: Defender responds to each valid charge
        defenses: list[DefenseResponse] = []
        if valid_charges:
            defender_out = await run_defender(
                pr_title=pr_ctx.pr_title,
                pr_description=pr_ctx.pr_description,
                diff=pr_ctx.diff,
                head_files=head_files,
                valid_charges=valid_charges,
                test_results=valid_test_results,
            )
            defenses = defender_out.responses
            for d in defenses:
                yield record_and_format("defense", d.model_dump())

        # Step 5: Apply one combined fix per file, re-run charge tests & existing tests
        fix_defenses = [
            d
            for d in defenses
            if d.stance == "fix" and d.fixed_file_path and d.fixed_code is not None
        ]

        after_fix_results: dict[str, TestResult] = {}
        fixed_charge_ids: set[str] = set()
        existing_tests_passed = True
        fix_rejected = False
        fixes_applied_count = 0

        if fix_defenses:
            fixes_by_file: dict[str, list[DefenseResponse]] = {}
            for d in fix_defenses:
                assert d.fixed_file_path is not None
                fixes_by_file.setdefault(d.fixed_file_path, []).append(d)

            file_backups: dict[Path, Optional[str]] = {}
            file_diffs: dict[str, str] = {}

            for rel_path, file_resps in fixes_by_file.items():
                target_file = (pr_ctx.head_dir / rel_path).resolve()
                if not str(target_file).startswith(str(pr_ctx.head_dir.resolve())):
                    continue
                old_content = (
                    target_file.read_text(encoding="utf-8")
                    if target_file.is_file()
                    else ""
                )
                file_backups[target_file] = old_content if target_file.is_file() else None
                combined_code = combine_file_fixes(
                    old_content,
                    [r.fixed_code for r in file_resps if r.fixed_code is not None],
                )
                target_file.parent.mkdir(parents=True, exist_ok=True)
                target_file.write_text(combined_code, encoding="utf-8")
                file_diffs[rel_path] = compute_unified_diff(
                    rel_path, old_content, combined_code
                )

            # Re-run charge tests for fixed charges in parallel
            fixed_charges_to_test = [
                charges_by_id[d.charge_id]
                for d in fix_defenses
                if d.charge_id in charges_by_id
            ]
            retest_tasks = [
                run_charge_test_after_fix(
                    c,
                    head_dir=pr_ctx.head_dir,
                    new_code=valid_test_results[c.id].new_code
                    if c.id in valid_test_results
                    else False,
                )
                for c in fixed_charges_to_test
            ]
            retest_list = list(await asyncio.gather(*retest_tasks))
            for r in retest_list:
                after_fix_results[r.charge_id] = r
                yield record_and_format("test_result", r.model_dump())

            # Run existing repository tests to check for regressions
            existing_tests_passed, _ = await run_existing_tests(pr_ctx.head_dir)

            if not existing_tests_passed:
                fix_rejected = True
                # Revert applied fixes on regression
                for path, backup in file_backups.items():
                    if backup is None:
                        path.unlink(missing_ok=True)
                    else:
                        path.write_text(backup, encoding="utf-8")
            else:
                for d in fix_defenses:
                    after_r = after_fix_results.get(d.charge_id)
                    if after_r and after_r.status == "pass":
                        fixed_charge_ids.add(d.charge_id)
                        fixes_applied_count += 1
                    else:
                        fix_rejected = True

            for d in fix_defenses:
                diff_str = file_diffs.get(d.fixed_file_path or "", "")
                yield record_and_format(
                    "fix_applied",
                    {
                        "charge_id": d.charge_id,
                        "diff": diff_str,
                        "existing_tests_passed": existing_tests_passed,
                    },
                )

        # Step 6: Judge rules on rebuttals and writes ruling; backend computes verdict & confidence
        judge_out = await run_judge(
            pr_title=pr_ctx.pr_title,
            pr_description=pr_ctx.pr_description,
            valid_charges=valid_charges,
            defenses=defenses,
            after_fix_results=after_fix_results,
            existing_tests_passed=existing_tests_passed,
        )

        # Enforce hard rule: a rebuttal without quoted_intent cannot be upheld
        defenses_by_id = {d.charge_id: d for d in defenses}
        sanitized_rulings: list[RebuttalRuling] = []
        for ruling in judge_out.rebuttal_rulings:
            d = defenses_by_id.get(ruling.charge_id)
            if d is None or d.stance != "rebut" or not (d.quoted_intent or "").strip():
                sanitized_rulings.append(
                    RebuttalRuling(
                        charge_id=ruling.charge_id,
                        upheld=False,
                        reason=ruling.reason
                        or "Rebuttal lacked required quoted intent from PR description or docstring.",
                    )
                )
            else:
                sanitized_rulings.append(ruling)
        judge_out.rebuttal_rulings = sanitized_rulings

        verdict, confidence = compute_verdict(
            valid_charges=valid_charges,
            rebuttal_rulings=judge_out.rebuttal_rulings,
            fixed_charge_ids=fixed_charge_ids,
            existing_tests_passed=existing_tests_passed,
            fix_rejected=fix_rejected,
        )

        duration_s = round(time.monotonic() - start_time, 2)
        stats = {
            "duration_s": duration_s,
            "charges_filed": len(charges),
            "charges_valid": len(valid_charges),
            "charges_dropped": charges_dropped_count,
            "fixes_applied": fixes_applied_count,
            "existing_tests_passed": existing_tests_passed,
        }

        verdict_payload = {
            **judge_out.model_dump(),
            "verdict": verdict,
            "confidence": confidence,
            "stats": stats,
        }
        yield record_and_format("verdict", verdict_payload)

        # Save live run automatically as a replay fixture
        save_fixture(pr_ctx.pr_url, recorded_events)

    except (ProsecutorAgentError, DefenderAgentError, JudgeAgentError) as exc:
        yield format_sse("error", {"message": str(exc)})
    except Exception as exc:
        yield format_sse("error", {"message": f"Trial failed: {exc}"})
