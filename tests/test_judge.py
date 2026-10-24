import asyncio
from unittest.mock import AsyncMock, patch

from backend.agents.judge import (
    SYSTEM_INSTRUCTION,
    build_computed_context,
    judge,
    sanitize_rebuttal_rulings,
)
from backend.models import (
    Charge,
    DefenseResponse,
    JudgeOutput,
    RebuttalRuling,
    TestResult,
)


def test_judge_system_instruction_exact():
    assert "You are the JUDGE in a code trial." in SYSTEM_INSTRUCTION
    assert "UNTRUSTED DATA" in SYSTEM_INSTRUCTION
    assert "The final verdict is decided by the court clerk (it is given to you)." in SYSTEM_INSTRUCTION
    assert "Return JSON only, matching the schema." in SYSTEM_INSTRUCTION


def test_build_computed_context_includes_verdict_candidates():
    c1 = Charge(
        id="C1",
        title="Empty cart",
        severity="high",
        explanation="Divides by zero",
        target_symbol="shop.cart.cart_total",
        intended_behavior="Return 0",
        test_code="def test_e(): assert False",
    )
    c2 = Charge(
        id="C2",
        title="Negative qty",
        severity="low",
        explanation="Raises ValueError",
        target_symbol="shop.cart.apply_discount",
        intended_behavior="Allow negative",
        test_code="def test_n(): assert False",
    )
    responses = [
        DefenseResponse(
            charge_id="C1",
            stance="fix",
            reasoning="Fixed empty cart",
            fixed_file_path="shop/cart.py",
            fixed_code="...",
        ),
        DefenseResponse(
            charge_id="C2",
            stance="rebut",
            reasoning="Documented in docstring",
            quoted_intent="Negative quantities are rejected with ValueError",
        ),
    ]
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
    after_fix = [
        TestResult(
            charge_id="C1",
            status="pass",
            new_code=False,
            output="1 passed",
            phase="after_fix",
        )
    ]

    ctx = build_computed_context([c1, c2], responses, fix_results, after_fix, True)
    assert ctx["rebuttal_charge_ids"] == ["C2"]
    by_c2 = {item["rebuttals"]["C2"]: item for item in ctx["verdict_candidates"]}
    assert by_c2[True]["verdict"] == "merge_with_fixes"
    assert by_c2[True]["confidence"] == 0.90
    assert by_c2[False]["verdict"] == "block"
    assert by_c2[False]["confidence"] == 0.70


def test_sanitize_rebuttal_rulings_rejects_missing_quoted_intent():
    responses = [
        DefenseResponse(
            charge_id="C1",
            stance="rebut",
            reasoning="Trust me",
            quoted_intent=None,
        )
    ]
    rulings = [RebuttalRuling(charge_id="C1", upheld=True, reason="Accepted")]
    cleaned = sanitize_rebuttal_rulings(rulings, responses)
    assert len(cleaned) == 1
    assert cleaned[0].upheld is False


def test_judge_calls_generate_structured_with_judge_model():
    fake_out = JudgeOutput(
        rebuttal_rulings=[
            RebuttalRuling(
                charge_id="C2",
                upheld=True,
                reason="The docstring explicitly states negative quantities raise ValueError by design.",
            )
        ],
        ruling="Charge C1 was fixed and verified by sandbox execution, while Charge C2 was dismissed as intentional per the documented specification. The pull request is approved to merge with fixes.",
        summary="1 charge fixed, 1 rebuttal upheld.",
    )
    transcript = {
        "defender_responses": [
            DefenseResponse(
                charge_id="C2",
                stance="rebut",
                reasoning="Intentional",
                quoted_intent="Negative quantities are rejected with ValueError",
            )
        ]
    }
    with patch(
        "backend.agents.judge.generate_structured",
        new=AsyncMock(return_value=fake_out),
    ):
        out = asyncio.run(
            judge(
                pr={"title": "PR", "body": "Body"},
                transcript=transcript,
                computed_context={"verdict_candidates": []},
            )
        )
    assert out.rebuttal_rulings[0].upheld is True
    assert "merge with fixes" in out.ruling
