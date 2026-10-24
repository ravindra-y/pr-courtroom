import asyncio
import time
import uuid
from pathlib import Path
from typing import Any, Iterable, Optional

from backend.agents.defender import defend
from backend.agents.judge import build_computed_context, judge
from backend.agents.prosecutor import prosecute
from backend.github_client import prepare_workspace
from backend.models import Charge, DefenseResponse, TestResult
from backend.sandbox import (
    apply_fix_async,
    classify_all_async,
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
    grouped = group_fixes_by_file(responses)

    fix_results: list[dict[str, Any]] = []
    after_fix_results: list[TestResult] = []

    for file_path, group in grouped.items():
        charge_ids: list[str] = group["charge_ids"]
        fixed_code: str = group["fixed_code"]

        diff = await apply_fix_async(head_path, file_path, fixed_code)
        existing_passed, existing_output = await run_existing_tests_async(head_path)

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
                }
            )
            continue

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


async def run_full_trial(
    pr_url: str,
    run_id: Optional[str] = None,
) -> dict[str, Any]:
    """
    Execute the complete trial pipeline:
    1. Fetch PR & prepare workspace
    2. Prosecutor files charges
    3. Sandbox classifies charges in parallel
    4. Defender responds to valid charges & fix loop executes
    5. Judge rules on rebuttals & writes ruling
    6. Scoring deterministically computes verdict & confidence
    """
    start_ts = time.perf_counter()
    effective_run_id = run_id or f"trial_{uuid.uuid4().hex[:8]}"

    workspace = await prepare_workspace(pr_url, effective_run_id)
    prosecutor_output = await prosecute(workspace, workspace["head_dir"])
    charges = prosecutor_output.charges

    initial_results = await classify_all_async(
        charges,
        head_dir=workspace["head_dir"],
        base_dir=workspace["base_dir"],
    )

    valid_charges: list[Charge] = []
    charges_dropped = 0
    for charge, res in zip(charges, initial_results):
        if res.status == "fail_valid":
            valid_charges.append(charge)
        else:
            charges_dropped += 1

    defense_stage = await run_defense_and_fixes(
        pr=workspace,
        valid_charges=valid_charges,
        test_results=initial_results,
        head_dir=workspace["head_dir"],
    )

    responses: list[DefenseResponse] = defense_stage["responses"]
    fix_results: list[dict[str, Any]] = defense_stage["fix_results"]
    after_fix_results: list[TestResult] = defense_stage["after_fix_results"]

    existing_tests_passed = all(
        fx.get("existing_tests_passed", True) for fx in fix_results
    )
    fixes_applied = sum(
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
        "charges_dropped": charges_dropped,
        "fixes_applied": fixes_applied,
        "existing_tests_passed": existing_tests_passed,
    }

    return {
        "pr": workspace,
        "charges": charges,
        "initial_results": initial_results,
        "valid_charges": valid_charges,
        "defender_responses": responses,
        "fix_results": fix_results,
        "after_fix_results": after_fix_results,
        "judge_output": judge_output,
        "verdict": verdict,
        "confidence": confidence,
        "stats": stats,
        "duration_s": duration_s,
    }
