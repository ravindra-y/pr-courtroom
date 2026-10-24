from backend.models import Charge, RebuttalRuling, TestResult
from backend.scoring import compute_verdict, standing_charges


def _charge(cid: str, severity: str = "medium") -> Charge:
    return Charge(
        id=cid,
        title=f"Charge {cid}",
        severity=severity,  # type: ignore[arg-type]
        explanation="Defect explanation",
        target_symbol="shop.cart.cart_total",
        intended_behavior="Expected behavior",
        test_code="def test_bug(): assert False",
    )


def _after_fix_result(cid: str, status: str = "pass") -> TestResult:
    return TestResult(
        charge_id=cid,
        status=status,  # type: ignore[arg-type]
        new_code=False,
        output="1 passed",
        phase="after_fix",
    )


def test_standing_charges_filters_upheld_rebuttals():
    c1 = _charge("C1", "high")
    c2 = _charge("C2", "medium")
    c3 = _charge("C3", "low")
    rulings = [
        RebuttalRuling(charge_id="C2", upheld=True, reason="Documented behavior"),
        RebuttalRuling(charge_id="C3", upheld=False, reason="Not in spec"),
    ]
    standing = standing_charges([c1, c2, c3], rulings)
    assert [c.id for c in standing] == ["C1", "C3"]


def test_no_valid_evidence_returns_merge_050():
    verdict, confidence = compute_verdict(
        standing=[],
        fix_results=[],
        after_fix_results=[],
        any_valid_evidence=False,
        dismissed_by_rebuttal=False,
        existing_tests_passed=True,
    )
    assert verdict == "merge"
    assert confidence == 0.50


def test_upheld_rebuttal_only_returns_merge_080():
    c1 = _charge("C1", "medium")
    rulings = [RebuttalRuling(charge_id="C1", upheld=True, reason="Intentional")]
    standing = standing_charges([c1], rulings)
    assert standing == []

    verdict, confidence = compute_verdict(
        standing=standing,
        fix_results=[],
        after_fix_results=[],
        any_valid_evidence=True,
        dismissed_by_rebuttal=True,
        existing_tests_passed=True,
    )
    assert verdict == "merge"
    assert confidence == 0.80


def test_all_standing_charges_fixed_returns_merge_with_fixes_090():
    c1 = _charge("C1", "high")
    c2 = _charge("C2", "low")
    rulings = [RebuttalRuling(charge_id="C2", upheld=True, reason="Intentional")]
    standing = standing_charges([c1, c2], rulings)

    fix_results = [
        {
            "file_path": "shop/cart.py",
            "charge_ids": ["C1"],
            "status": "applied",
            "applied": True,
            "rejected_for_regression": False,
            "existing_tests_passed": True,
        }
    ]
    after_fix = [_after_fix_result("C1", "pass")]

    verdict, confidence = compute_verdict(
        standing=standing,
        fix_results=fix_results,
        after_fix_results=after_fix,
        any_valid_evidence=True,
        dismissed_by_rebuttal=True,
        existing_tests_passed=True,
    )
    assert verdict == "merge_with_fixes"
    assert confidence == 0.90


def test_unfixed_medium_standing_charge_returns_block_070():
    c1 = _charge("C1", "medium")
    verdict, confidence = compute_verdict(
        standing=[c1],
        fix_results=[],
        after_fix_results=[],
        any_valid_evidence=True,
        dismissed_by_rebuttal=False,
        existing_tests_passed=True,
    )
    assert verdict == "block"
    assert confidence == 0.70


def test_unfixed_high_severity_charge_returns_block_080():
    c1 = _charge("C1", "high")
    verdict, confidence = compute_verdict(
        standing=[c1],
        fix_results=[],
        after_fix_results=[],
        any_valid_evidence=True,
        dismissed_by_rebuttal=False,
        existing_tests_passed=True,
    )
    assert verdict == "block"
    assert confidence == 0.80


def test_fix_rejected_for_regression_returns_block():
    c1 = _charge("C1", "medium")
    fix_results = [
        {
            "file_path": "shop/cart.py",
            "charge_ids": ["C1"],
            "status": "rejected_for_regression",
            "applied": False,
            "rejected_for_regression": True,
            "existing_tests_passed": False,
        }
    ]
    verdict, confidence = compute_verdict(
        standing=[c1],
        fix_results=fix_results,
        after_fix_results=[_after_fix_result("C1", "pass")],
        any_valid_evidence=True,
        dismissed_by_rebuttal=False,
        existing_tests_passed=False,
    )
    assert verdict == "block"
    assert confidence == 0.70
