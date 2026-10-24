import asyncio
from pathlib import Path
from typing import Any, Iterable, Optional

from backend.agents.defender import defend
from backend.models import Charge, DefenseResponse, TestResult
from backend.sandbox import (
    apply_fix_async,
    revert_fix,
    run_after_fix_async,
    run_existing_tests_async,
)


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
