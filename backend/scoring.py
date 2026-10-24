from typing import Any, Iterable, Literal
from backend.models import Charge, RebuttalRuling, TestResult

VerdictType = Literal["merge", "merge_with_fixes", "block"]


def standing_charges(
    valid_charges: list[Charge],
    rebuttal_rulings: Iterable[RebuttalRuling],
) -> list[Charge]:
    """Return valid_charges that were NOT dismissed by an upheld rebuttal."""
    upheld_ids = {r.charge_id for r in rebuttal_rulings if r.upheld}
    return [c for c in valid_charges if c.id not in upheld_ids]


def _extract_after_fix_passed_ids(
    after_fix_results: Iterable[TestResult] | dict[str, TestResult] | None,
) -> set[str]:
    if not after_fix_results:
        return set()
    items = (
        after_fix_results.values()
        if isinstance(after_fix_results, dict)
        else after_fix_results
    )
    return {
        r.charge_id
        for r in items
        if isinstance(r, TestResult) and r.phase == "after_fix" and r.status == "pass"
    }


def compute_verdict(
    standing: list[Charge],
    fix_results: list[dict[str, Any]] | None,
    after_fix_results: Iterable[TestResult] | dict[str, TestResult] | None,
    any_valid_evidence: bool,
    dismissed_by_rebuttal: bool,
    existing_tests_passed: bool = True,
) -> tuple[VerdictType, float]:
    """
    Compute final courtroom (verdict, confidence) deterministically per AGENTS.md:
    - No standing charges:
      verdict "merge"; confidence 0.50 if no valid evidence was found at all,
      0.80 if charges were dismissed by upheld rebuttals.
    - All standing charges fixed and charge tests + existing tests pass:
      "merge_with_fixes", 0.90.
    - Any standing charge unfixed, or a fix rejected for regression:
      "block", 0.70 (0.80 if an unfixed charge is high severity).
    """
    fix_list = fix_results or []
    fix_rejected_for_regression = (not existing_tests_passed) or any(
        bool(fx.get("rejected_for_regression"))
        or fx.get("status") == "rejected_for_regression"
        or fx.get("existing_tests_passed") is False
        for fx in fix_list
        if isinstance(fx, dict)
    )

    if not standing:
        if not fix_rejected_for_regression:
            if dismissed_by_rebuttal or any_valid_evidence:
                return "merge", 0.80
            return "merge", 0.50
        return "block", 0.70

    passed_after_fix_ids = _extract_after_fix_passed_ids(after_fix_results)

    applied_charge_ids: set[str] = set()
    if fix_list:
        for fx in fix_list:
            if not isinstance(fx, dict):
                continue
            if (
                fx.get("applied", False)
                and not fx.get("rejected_for_regression", False)
                and fx.get("existing_tests_passed", True)
            ):
                for cid in fx.get("charge_ids", []):
                    if cid in passed_after_fix_ids:
                        applied_charge_ids.add(cid)
    else:
        applied_charge_ids = set(passed_after_fix_ids)

    if fix_rejected_for_regression or not existing_tests_passed:
        effective_fixed_ids: set[str] = set()
    else:
        effective_fixed_ids = applied_charge_ids

    unfixed_standing = [c for c in standing if c.id not in effective_fixed_ids]

    if not unfixed_standing and existing_tests_passed and not fix_rejected_for_regression:
        return "merge_with_fixes", 0.90

    if not unfixed_standing:
        unfixed_standing = list(standing)

    has_high_unfixed = any(c.severity == "high" for c in unfixed_standing)
    return "block", (0.80 if has_high_unfixed else 0.70)
