from backend.models import Charge, RebuttalRuling
from backend.scoring import compute_verdict


def _make_charge(cid: str, severity: str = "medium") -> Charge:
    return Charge(
        id=cid,
        title=f"Charge {cid}",
        severity=severity,  # type: ignore[arg-type]
        explanation="Bug explanation",
        target_symbol="pkg.mod.fn",
        intended_behavior="Should work",
        test_code="def test_x(): assert False",
    )


def test_no_valid_charges_returns_merge_050():
    verdict, confidence = compute_verdict(
        valid_charges=[],
        rebuttal_rulings=[],
        fixed_charge_ids=set(),
    )
    assert verdict == "merge"
    assert confidence == 0.50


def test_all_valid_charges_dismissed_by_rebuttal_returns_merge_080():
    c1 = _make_charge("C1", "high")
    c2 = _make_charge("C2", "low")
    rulings = [
        RebuttalRuling(charge_id="C1", upheld=True, reason="Documented behavior"),
        RebuttalRuling(charge_id="C2", upheld=True, reason="Intentional change"),
    ]
    verdict, confidence = compute_verdict(
        valid_charges=[c1, c2],
        rebuttal_rulings=rulings,
        fixed_charge_ids=set(),
    )
    assert verdict == "merge"
    assert confidence == 0.80


def test_standing_charges_all_fixed_returns_merge_with_fixes_090():
    c1 = _make_charge("C1", "high")
    c2 = _make_charge("C2", "medium")
    rulings = [
        RebuttalRuling(charge_id="C1", upheld=True, reason="Dismissed"),
    ]
    verdict, confidence = compute_verdict(
        valid_charges=[c1, c2],
        rebuttal_rulings=rulings,
        fixed_charge_ids={"C2"},
        existing_tests_passed=True,
    )
    assert verdict == "merge_with_fixes"
    assert confidence == 0.90


def test_unfixed_medium_charge_returns_block_070():
    c1 = _make_charge("C1", "medium")
    rulings = [
        RebuttalRuling(charge_id="C1", upheld=False, reason="Rebuttal overruled"),
    ]
    verdict, confidence = compute_verdict(
        valid_charges=[c1],
        rebuttal_rulings=rulings,
        fixed_charge_ids=set(),
    )
    assert verdict == "block"
    assert confidence == 0.70


def test_unfixed_high_charge_returns_block_080():
    c1 = _make_charge("C1", "high")
    verdict, confidence = compute_verdict(
        valid_charges=[c1],
        rebuttal_rulings=[],
        fixed_charge_ids=set(),
    )
    assert verdict == "block"
    assert confidence == 0.80


def test_fix_rejected_for_regression_returns_block():
    c1 = _make_charge("C1", "low")
    verdict, confidence = compute_verdict(
        valid_charges=[c1],
        rebuttal_rulings=[],
        fixed_charge_ids={"C1"},
        existing_tests_passed=False,
    )
    assert verdict == "block"
    assert confidence == 0.70
