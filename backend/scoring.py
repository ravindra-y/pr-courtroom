from typing import Iterable, Literal
from backend.models import Charge, RebuttalRuling

VerdictType = Literal["merge", "merge_with_fixes", "block"]


def compute_verdict(
    valid_charges: list[Charge],
    rebuttal_rulings: list[RebuttalRuling],
    fixed_charge_ids: Iterable[str],
    existing_tests_passed: bool = True,
    fix_rejected: bool = False,
) -> tuple[VerdictType, float]:
    """Compute final courtroom verdict and confidence score deterministically."""
    upheld_ids = {r.charge_id for r in rebuttal_rulings if r.upheld}
    standing_charges = [c for c in valid_charges if c.id not in upheld_ids]

    if not standing_charges:
        if not valid_charges:
            return "merge", 0.50
        return "merge", 0.80

    fixed_set = set(fixed_charge_ids) if existing_tests_passed else set()
    unfixed_charges = [c for c in standing_charges if c.id not in fixed_set]

    if not unfixed_charges and existing_tests_passed and not fix_rejected:
        return "merge_with_fixes", 0.90

    if not unfixed_charges:
        unfixed_charges = list(standing_charges)

    has_high_severity_unfixed = any(c.severity == "high" for c in unfixed_charges)
    confidence = 0.80 if has_high_severity_unfixed else 0.70
    return "block", confidence


compute_verdict_and_confidence = compute_verdict
