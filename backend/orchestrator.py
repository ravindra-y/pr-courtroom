import asyncio
import re
import shutil
import time
import uuid
from pathlib import Path
from typing import Any, AsyncIterator, Iterable, Optional

from backend.agents.defender import defend
from backend.agents.gemini_client import GeminiClientError, redact_secrets
from backend.agents.judge import build_computed_context, judge
from backend.agents.prosecutor import prosecute
from backend.github_client import GitHubClientError, WORKDIR_ROOT, parse_pr_url, prepare_workspace
from backend.models import Charge, DefenseResponse, JudgeOutput, TestResult
from backend.replay import clamp_delay, save_fixture
from backend.sandbox import (
    apply_fix_async,
    classify_all_async,
    classify_async,
    revert_fix,
    run_after_fix_async,
    run_existing_tests_async,
)
from backend.scoring import compute_verdict, standing_charges


def group_fixes_by_file(
    responses: Iterable[DefenseResponse],
) -> dict[str, dict[str, Any]]:
    """
    Group 'fix' DefenseResponses by `fixed_file_path`.
    If several fix responses target the same file, use the last complete `fixed_code`.
    Returns {file_path: {"file_path": str, "fixed_code": str, "charge_ids": list[str]}}.
    """
    grouped: dict[str, dict[str, Any]] = {}
    for resp in responses:
        if resp.stance != "fix":
            continue
        file_path = (resp.fixed_file_path or "").strip()
        if not file_path or resp.fixed_code is None:
            continue
        if file_path not in grouped:
            grouped[file_path] = {
                "file_path": file_path,
                "fixed_code": resp.fixed_code,
                "charge_ids": [resp.charge_id],
            }
        else:
            grouped[file_path]["fixed_code"] = resp.fixed_code
            if resp.charge_id not in grouped[file_path]["charge_ids"]:
                grouped[file_path]["charge_ids"].append(resp.charge_id)
    return grouped


async def execute_fix_loop(
    valid_charges: list[Charge],
    responses: list[DefenseResponse],
    head_dir: str | Path,
    tests_dir: Optional[str | Path] = None,
) -> dict[str, Any]:
    """
    Apply each grouped file fix with `sandbox.apply_fix`, then `run_existing_tests` on head.
    If existing tests fail, revert that fix (restore backup) and mark the fix as
    `rejected_for_regression`. If they pass, re-run each related charge test with
    `run_after_fix` and record the TestResult (phase="after_fix").

    Returns:
      {
        "responses": list[DefenseResponse],
        "fix_results": list[dict],
        "after_fix_results": list[TestResult],
      }
    """
    head_path = Path(head_dir).resolve()
    charges_by_id = {c.id: c for c in valid_charges}
    initial_new_code_by_id: dict[str, bool] = {}
    grouped = group_fixes_by_file(responses)

    fix_results: list[dict[str, Any]] = []
    after_fix_results: list[TestResult] = []

    for file_path, group in grouped.items():
        charge_ids: list[str] = group["charge_ids"]
        fixed_code: str = group["fixed_code"]

        diff = await apply_fix_async(head_path, file_path, fixed_code)
        existing_passed, existing_output = await run_existing_tests_async(head_path)

        related_charges = [
            charges_by_id[cid] for cid in charge_ids if cid in charges_by_id
        ]
        retest_results = list(
            await asyncio.gather(
                *(
                    run_after_fix_async(c, head_path, tests_dir)
                    for c in related_charges
                )
            )
        )
        for r in retest_results:
            if r.charge_id in initial_new_code_by_id:
                r.new_code = initial_new_code_by_id[r.charge_id]

        if not existing_passed:
            await asyncio.to_thread(revert_fix, head_path, file_path)
            fix_results.append(
                {
                    "file_path": file_path,
                    "charge_ids": charge_ids,
                    "status": "rejected_for_regression",
                    "applied": False,
                    "rejected_for_regression": True,
                    "diff": diff,
                    "existing_tests_passed": False,
                    "existing_tests_output": existing_output,
                    "after_fix_results": retest_results,
                }
            )
            after_fix_results.extend(retest_results)
            continue

        after_fix_results.extend(retest_results)
        all_charges_passed = all(r.status == "pass" for r in retest_results)
        if not all_charges_passed:
            await asyncio.to_thread(revert_fix, head_path, file_path)
            fix_results.append(
                {
                    "file_path": file_path,
                    "charge_ids": charge_ids,
                    "status": "rejected_charge_still_fails",
                    "applied": False,
                    "rejected_for_regression": False,
                    "diff": diff,
                    "existing_tests_passed": True,
                    "existing_tests_output": existing_output,
                    "after_fix_results": retest_results,
                }
            )
        else:
            fix_results.append(
                {
                    "file_path": file_path,
                    "charge_ids": charge_ids,
                    "status": "applied",
                    "applied": True,
                    "rejected_for_regression": False,
                    "diff": diff,
                    "existing_tests_passed": True,
                    "existing_tests_output": existing_output,
                    "after_fix_results": retest_results,
                }
            )

    return {
        "responses": responses,
        "fix_results": fix_results,
        "after_fix_results": after_fix_results,
    }


async def run_defense_and_fixes(
    pr: dict,
    valid_charges: list[Charge],
    test_results: dict[str, TestResult] | list[TestResult],
    head_dir: str | Path,
    tests_dir: Optional[str | Path] = None,
) -> dict[str, Any]:
    """Run the Defender agent and execute the fix verification loop."""
    defender_output = await defend(
        pr=pr,
        valid_charges=valid_charges,
        test_results=test_results,
        head_dir=head_dir,
    )
    return await execute_fix_loop(
        valid_charges=valid_charges,
        responses=defender_output.responses,
        head_dir=head_dir,
        tests_dir=tests_dir,
    )


def _drop_reason(status: str) -> Optional[str]:
    if status == "pass":
        return "overruled by evidence"
    if status in ("error", "timeout"):
        return "broken test"
    if status == "fail_preexisting":
        return "pre-existing"
    return None


def _sanitize_error_message(exc: Exception) -> str:
    """Return a clean, user-friendly error message without stack traces or secrets."""
    if isinstance(exc, (ValueError, GitHubClientError, GeminiClientError)):
        msg = str(exc)
    else:
        msg = f"Trial execution encountered an unexpected error ({type(exc).__name__})."
    return redact_secrets(msg)


async def run_trial(
    pr_url: str,
    run_id: Optional[str] = None,
) -> AsyncIterator[tuple[str, dict[str, Any]]]:
    """
    Async generator that orchestrates a full trial and yields (event_name, data_dict)
    in the exact order specified by AGENTS.md:
      1. trial_started {pr_title, files_changed, pr_url}
      2. charge_filed {Charge}
      3. test_result {TestResult} (and charge_dropped {charge_id, reason} if dropped)
      4. defense {DefenseResponse}
      5. fix_applied {charge_id, diff, existing_tests_passed}, then test_result (phase "after_fix")
      6. verdict {JudgeOutput fields, verdict, confidence, stats}
      7. error {message} on any exception
    Saves all yielded events with clamped delays (0.5..2.0s) to backend/fixtures/<run_id>.json
    and cleans up workdir/<run_id> at the end.
    """
    effective_run_id = run_id or f"run_{uuid.uuid4().hex[:8]}"
    start_ts = time.perf_counter()
    prev_ts = start_ts
    recorded: list[dict[str, Any]] = []

    def record(event_name: str, payload: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        nonlocal prev_ts
        now = time.perf_counter()
        delay = clamp_delay(now - prev_ts)
        prev_ts = now
        recorded.append({"event": event_name, "data": payload, "delay": delay})
        return event_name, payload

    try:
        parse_pr_url(pr_url)
        workspace = await prepare_workspace(pr_url, effective_run_id)

        py_files = [
            f for f in workspace.get("changed_files", []) if str(f).endswith(".py")
        ]
        if not py_files:
            raise ValueError(
                "This pull request does not modify any Python (.py) files. PR Courtroom only trials Python code changes."
            )

        yield record(
            "trial_started",
            {
                "pr_title": workspace["title"],
                "files_changed": workspace["changed_files"],
                "pr_url": pr_url,
            },
        )

        prosecutor_output = await prosecute(workspace, workspace["head_dir"])
        charges = prosecutor_output.charges[:3]

        for charge in charges:
            yield record("charge_filed", charge.model_dump())

        # Run initial evidence tests in parallel and yield each as it finishes
        tasks = [
            asyncio.create_task(
                classify_async(
                    charge,
                    head_dir=workspace["head_dir"],
                    base_dir=workspace["base_dir"],
                )
            )
            for charge in charges
        ]

        initial_results: list[TestResult] = []
        initial_by_id: dict[str, TestResult] = {}
        charges_dropped_count = 0

        for finished_task in asyncio.as_completed(tasks):
            res = await finished_task
            initial_results.append(res)
            initial_by_id[res.charge_id] = res
            yield record("test_result", res.model_dump())

            reason = _drop_reason(res.status)
            if reason is not None:
                charges_dropped_count += 1
                yield record(
                    "charge_dropped",
                    {"charge_id": res.charge_id, "reason": reason},
                )

        # Preserve charge ordering for valid charges
        valid_charges = [
            c
            for c in charges
            if c.id in initial_by_id and initial_by_id[c.id].status == "fail_valid"
        ]

        # Edge case: all charges broken / dropped (no valid evidence survived)
        if not valid_charges:
            verdict, confidence = compute_verdict(
                standing=[],
                fix_results=[],
                after_fix_results=[],
                any_valid_evidence=False,
                dismissed_by_rebuttal=False,
                existing_tests_passed=True,
            )
            duration_s = round(time.perf_counter() - start_ts, 2)
            stats = {
                "duration_s": duration_s,
                "charges_filed": len(charges),
                "charges_valid": 0,
                "charges_dropped": charges_dropped_count,
                "fixes_applied": 0,
                "existing_tests_passed": True,
            }
            judge_output = JudgeOutput(
                rebuttal_rulings=[],
                ruling=(
                    "No charges were proven by executable pytest evidence in the sandbox "
                    "(all filed charges were dropped as broken tests, overruled by passing "
                    "evidence, or pre-existing). With no standing defects proven, the pull "
                    "request is cleared to merge."
                ),
                summary="No valid sandbox evidence survived; merge (confidence 0.50).",
            )
            verdict_data = {
                **judge_output.model_dump(),
                "verdict": verdict,
                "confidence": confidence,
                "stats": stats,
            }
            yield record("verdict", verdict_data)
            save_fixture(effective_run_id, recorded)
            return

        responses: list[DefenseResponse] = []
        if valid_charges:
            defender_output = await defend(
                pr=workspace,
                valid_charges=valid_charges,
                test_results=initial_by_id,
                head_dir=workspace["head_dir"],
            )
            responses = defender_output.responses
            for resp in responses:
                yield record("defense", resp.model_dump())

        fix_loop_out = await execute_fix_loop(
            valid_charges=valid_charges,
            responses=responses,
            head_dir=workspace["head_dir"],
        )
        fix_results: list[dict[str, Any]] = fix_loop_out["fix_results"]
        after_fix_results: list[TestResult] = fix_loop_out["after_fix_results"]
        after_fix_by_id = {r.charge_id: r for r in after_fix_results}

        for fx in fix_results:
            diff_str = fx.get("diff", "")
            existing_passed = bool(fx.get("existing_tests_passed", True))
            for cid in fx.get("charge_ids", []):
                yield record(
                    "fix_applied",
                    {
                        "charge_id": cid,
                        "diff": diff_str,
                        "existing_tests_passed": existing_passed,
                    },
                )
                after_tr = after_fix_by_id.get(cid)
                if after_tr is not None:
                    if cid in initial_by_id:
                        after_tr.new_code = initial_by_id[cid].new_code
                    yield record("test_result", after_tr.model_dump())

        existing_tests_passed = all(
            fx.get("existing_tests_passed", True) for fx in fix_results
        )
        fixes_applied_count = sum(
            len(fx.get("charge_ids", [])) for fx in fix_results if fx.get("applied")
        )

        transcript = {
            "charges": charges,
            "valid_charges": valid_charges,
            "initial_test_results": initial_results,
            "defender_responses": responses,
            "fix_results": fix_results,
            "after_fix_test_results": after_fix_results,
        }
        computed_context = build_computed_context(
            valid_charges=valid_charges,
            defender_responses=responses,
            fix_results=fix_results,
            after_fix_results=after_fix_results,
            existing_tests_passed=existing_tests_passed,
        )

        judge_output = await judge(
            pr=workspace,
            transcript=transcript,
            computed_context=computed_context,
        )

        standing = standing_charges(valid_charges, judge_output.rebuttal_rulings)
        dismissed_by_rebuttal = any(r.upheld for r in judge_output.rebuttal_rulings)
        verdict, confidence = compute_verdict(
            standing=standing,
            fix_results=fix_results,
            after_fix_results=after_fix_results,
            any_valid_evidence=len(valid_charges) > 0,
            dismissed_by_rebuttal=dismissed_by_rebuttal,
            existing_tests_passed=existing_tests_passed,
        )

        duration_s = round(time.perf_counter() - start_ts, 2)
        stats = {
            "duration_s": duration_s,
            "charges_filed": len(charges),
            "charges_valid": len(valid_charges),
            "charges_dropped": charges_dropped_count,
            "fixes_applied": fixes_applied_count,
            "existing_tests_passed": existing_tests_passed,
        }

        verdict_data = {
            **judge_output.model_dump(),
            "verdict": verdict,
            "confidence": confidence,
            "stats": stats,
        }
        yield record("verdict", verdict_data)

        save_fixture(effective_run_id, recorded)

    except Exception as exc:
        yield ("error", {"message": _sanitize_error_message(exc)})
    finally:
        run_workdir = WORKDIR_ROOT / str(effective_run_id)
        if run_workdir.exists():
            shutil.rmtree(run_workdir, ignore_errors=True)


async def run_full_trial(
    pr_url: str,
    run_id: Optional[str] = None,
) -> dict[str, Any]:
    """Run a trial to completion and return a summary dict (used by run_trial.py CLI)."""
    effective_run_id = run_id or f"trial_{uuid.uuid4().hex[:8]}"
    events: list[tuple[str, dict[str, Any]]] = []
    async for ev_name, ev_data in run_trial(pr_url, effective_run_id):
        events.append((ev_name, ev_data))
        if ev_name == "error":
            raise GeminiClientError(ev_data["message"])

    started = next(d for n, d in events if n == "trial_started")
    verdict_ev = next(d for n, d in events if n == "verdict")
    from backend.models import JudgeOutput

    judge_out = JudgeOutput(
        rebuttal_rulings=verdict_ev["rebuttal_rulings"],
        ruling=verdict_ev["ruling"],
        summary=verdict_ev["summary"],
    )
    return {
        "pr": {"title": started["pr_title"], "changed_files": started["files_changed"]},
        "judge_output": judge_out,
        "verdict": verdict_ev["verdict"],
        "confidence": verdict_ev["confidence"],
        "stats": verdict_ev["stats"],
        "duration_s": verdict_ev["stats"]["duration_s"],
        "events": events,
    }
