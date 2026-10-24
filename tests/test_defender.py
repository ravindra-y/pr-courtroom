import asyncio
import shutil
from pathlib import Path
from unittest.mock import AsyncMock, patch
import pytest

from backend.agents.defender import (
    SYSTEM_INSTRUCTION,
    build_defender_user_content,
    defend,
)
from backend.models import Charge, DefenderOutput, DefenseResponse, TestResult
from backend.orchestrator import execute_fix_loop, group_fixes_by_file

REPO_ROOT = Path(__file__).resolve().parent.parent
DEMO_BASE = REPO_ROOT / "demo-repo" / "base"
DEMO_HEAD = REPO_ROOT / "demo-repo" / "head"


def test_defender_system_instruction_exact():
    assert "You are the DEFENDER in a code trial." in SYSTEM_INSTRUCTION
    assert "UNTRUSTED DATA" in SYSTEM_INSTRUCTION
    assert "Stance 'rebut':" in SYSTEM_INSTRUCTION
    assert "Stance 'fix':" in SYSTEM_INSTRUCTION
    assert "If two charges are in the same file, return the same fixed_file_path with ONE fixed_code that fixes both." in SYSTEM_INSTRUCTION


def test_group_fixes_by_file_uses_last_complete_fixed_code():
    r1 = DefenseResponse(
        charge_id="C1",
        stance="fix",
        reasoning="Fix C1",
        fixed_file_path="shop/cart.py",
        fixed_code="# version 1",
    )
    r2 = DefenseResponse(
        charge_id="C2",
        stance="fix",
        reasoning="Fix C1 and C2 combined",
        fixed_file_path="shop/cart.py",
        fixed_code="# combined version 2",
    )
    r3 = DefenseResponse(
        charge_id="C3",
        stance="rebut",
        reasoning="Intentional per docstring",
        quoted_intent="Negative quantities are rejected with ValueError",
    )
    grouped = group_fixes_by_file([r1, r2, r3])
    assert list(grouped.keys()) == ["shop/cart.py"]
    assert grouped["shop/cart.py"]["fixed_code"] == "# combined version 2"
    assert grouped["shop/cart.py"]["charge_ids"] == ["C1", "C2"]


def test_execute_fix_loop_applies_valid_fix_and_turns_green(tmp_path: Path):
    head_dir = tmp_path / "head"
    shutil.copytree(DEMO_HEAD, head_dir)

    c1 = Charge(
        id="C1",
        title="Empty cart raises ZeroDivisionError",
        severity="high",
        explanation="Divides by len(items)",
        target_symbol="shop.cart.cart_total",
        intended_behavior="Return 0 on empty cart",
        test_code="from shop.cart import cart_total\ndef test_empty(): assert cart_total([]) == 0\n",
    )
    c2 = Charge(
        id="C2",
        title="Negative qty raises ValueError",
        severity="low",
        explanation="Rejects negative qty",
        target_symbol="shop.cart.apply_discount",
        intended_behavior="Allow negative qty",
        test_code=(
            "from shop.cart import apply_discount\n"
            "def test_neg(): assert apply_discount([{'name': 'b', 'price': 10.0, 'qty': -1}], 10) == -9.0\n"
        ),
    )

    fixed_cart = (
        "def cart_total(items):\n"
        "    if not items:\n"
        "        return 0\n"
        "    subtotal = sum(item['price'] * item['qty'] for item in items)\n"
        "    avg = subtotal / len(items)\n"
        "    if avg > 100:\n"
        "        return subtotal * 0.95\n"
        "    return subtotal\n\n\n"
        "def apply_discount(items, percent):\n"
        '    """Return the cart total after a percentage discount. percent must be between 0 and 100 inclusive. Negative quantities are rejected with ValueError; this is intentional because returns are handled by a separate refund flow."""\n'
        "    if not (0 <= percent <= 100):\n"
        "        raise ValueError('percent must be between 0 and 100 inclusive')\n"
        "    for item in items:\n"
        "        if item['qty'] < 0:\n"
        "            raise ValueError('Negative quantities are not allowed')\n"
        "    total = cart_total(items)\n"
        "    return total - total * percent / 100\n"
    )

    responses = [
        DefenseResponse(
            charge_id="C1",
            stance="fix",
            reasoning="Guard against empty items list in cart_total",
            fixed_file_path="shop/cart.py",
            fixed_code=fixed_cart,
        ),
        DefenseResponse(
            charge_id="C2",
            stance="rebut",
            reasoning="The docstring and PR description explicitly state that negative quantities raise ValueError by design.",
            quoted_intent="Negative quantities are rejected with ValueError; this is intentional because returns are handled by a separate refund flow.",
        ),
    ]

    result = asyncio.run(execute_fix_loop([c1, c2], responses, head_dir))
    assert len(result["fix_results"]) == 1
    fx = result["fix_results"][0]
    assert fx["status"] == "applied"
    assert fx["applied"] is True
    assert fx["rejected_for_regression"] is False
    assert fx["existing_tests_passed"] is True
    assert len(result["after_fix_results"]) == 1
    assert result["after_fix_results"][0].charge_id == "C1"
    assert result["after_fix_results"][0].status == "pass"
    assert result["after_fix_results"][0].phase == "after_fix"


def test_execute_fix_loop_rejects_and_reverts_on_regression(tmp_path: Path):
    head_dir = tmp_path / "head"
    shutil.copytree(DEMO_HEAD, head_dir)
    original_code = (head_dir / "shop" / "cart.py").read_text(encoding="utf-8")

    c1 = Charge(
        id="C1",
        title="Empty cart raises ZeroDivisionError",
        severity="high",
        explanation="Divides by len(items)",
        target_symbol="shop.cart.cart_total",
        intended_behavior="Return 0 on empty cart",
        test_code="from shop.cart import cart_total\ndef test_empty(): assert cart_total([]) == 0\n",
    )

    bad_fix = "def cart_total(items):\n    return 0\n"
    responses = [
        DefenseResponse(
            charge_id="C1",
            stance="fix",
            reasoning="Return 0",
            fixed_file_path="shop/cart.py",
            fixed_code=bad_fix,
        )
    ]

    result = asyncio.run(execute_fix_loop([c1], responses, head_dir))
    fx = result["fix_results"][0]
    assert fx["status"] == "rejected_for_regression"
    assert fx["rejected_for_regression"] is True
    assert fx["existing_tests_passed"] is False
    assert (head_dir / "shop" / "cart.py").read_text(encoding="utf-8") == original_code
