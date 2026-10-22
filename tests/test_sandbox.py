from pathlib import Path
import os
import pytest

from backend.models import Charge
from backend.sandbox import (
    classify_charge_test,
    classify_charges_parallel,
    run_charge_test_after_fix,
    run_existing_tests,
    symbol_exists_on_base,
)


def _setup_versions(tmp_path: Path) -> tuple[Path, Path]:
    base_dir = tmp_path / "base"
    head_dir = tmp_path / "head"
    (base_dir / "shop").mkdir(parents=True)
    (head_dir / "shop").mkdir(parents=True)
    (base_dir / "shop" / "__init__.py").write_text("", encoding="utf-8")
    (head_dir / "shop" / "__init__.py").write_text("", encoding="utf-8")

    # BASE version of shop/cart.py
    (base_dir / "shop" / "cart.py").write_text(
        """
def calc_subtotal(items):
    return sum(i["price"] * i["qty"] for i in items)

def buggy_legacy(x):
    return x / 0
""".strip()
        + "\n",
        encoding="utf-8",
    )

    # HEAD version of shop/cart.py:
    # - regression in calc_subtotal (ignores qty)
    # - new function apply_discount with a bug (ZeroDivisionError on 100% or negative)
    # - pre-existing buggy_legacy still broken
    (head_dir / "shop" / "cart.py").write_text(
        """
def calc_subtotal(items):
    return sum(i["price"] for i in items)

def apply_discount(total, code):
    if code == "SAVE20":
        return total * 0.8
    return total / 0

def buggy_legacy(x):
    return x / 0
""".strip()
        + "\n",
        encoding="utf-8",
    )
    return base_dir, head_dir


def test_symbol_exists_on_base_ast(tmp_path: Path):
    base_dir, _ = _setup_versions(tmp_path)
    assert symbol_exists_on_base(base_dir, "shop.cart.calc_subtotal") is True
    assert symbol_exists_on_base(base_dir, "shop.cart.apply_discount") is False
    assert symbol_exists_on_base(base_dir, "shop.nonexistent.fn") is False


@pytest.mark.asyncio
async def test_pass_on_head_is_overruled(tmp_path: Path):
    base_dir, head_dir = _setup_versions(tmp_path)
    charge = Charge(
        id="C1",
        title="SAVE20 works",
        severity="low",
        explanation="Test passes",
        target_symbol="shop.cart.apply_discount",
        intended_behavior="20% discount",
        test_code="""
from shop.cart import apply_discount
def test_save20():
    assert apply_discount(100, "SAVE20") == 80.0
""",
    )
    res = await classify_charge_test(charge, base_dir, head_dir)
    assert res.status == "pass"
    assert res.new_code is False
    assert res.phase == "initial"


@pytest.mark.asyncio
async def test_broken_tests_return_error(tmp_path: Path):
    base_dir, head_dir = _setup_versions(tmp_path)

    # 1. ImportError
    c_import = Charge(
        id="C_IMP",
        title="Bad import",
        severity="low",
        explanation="Import error",
        target_symbol="shop.cart.calc_subtotal",
        intended_behavior="...",
        test_code="from shop.cart import does_not_exist\ndef test_x(): pass\n",
    )
    # 2. SyntaxError
    c_syntax = Charge(
        id="C_SYN",
        title="Bad syntax",
        severity="low",
        explanation="Syntax error",
        target_symbol="shop.cart.calc_subtotal",
        intended_behavior="...",
        test_code="def test_x(\n    assert True\n",
    )
    # 3. Missing fixture
    c_fixture = Charge(
        id="C_FIX",
        title="Missing fixture",
        severity="low",
        explanation="Fixture error",
        target_symbol="shop.cart.calc_subtotal",
        intended_behavior="...",
        test_code="def test_x(missing_custom_fixture):\n    assert missing_custom_fixture\n",
    )

    results = await classify_charges_parallel(
        [c_import, c_syntax, c_fixture], base_dir, head_dir
    )
    for r in results:
        assert r.status == "error"


@pytest.mark.asyncio
async def test_timeout_returns_timeout(tmp_path: Path):
    base_dir, head_dir = _setup_versions(tmp_path)
    charge = Charge(
        id="C_TIME",
        title="Infinite loop",
        severity="medium",
        explanation="Hangs",
        target_symbol="shop.cart.calc_subtotal",
        intended_behavior="...",
        test_code="import time\ndef test_hang():\n    time.sleep(5)\n",
    )
    res = await classify_charge_test(charge, base_dir, head_dir, timeout=0.5)
    assert res.status == "timeout"


@pytest.mark.asyncio
async def test_new_code_defect_detected_via_ast(tmp_path: Path):
    base_dir, head_dir = _setup_versions(tmp_path)
    charge = Charge(
        id="C_NEW",
        title="Unhandled promo code crashes with ZeroDivisionError",
        severity="high",
        explanation="Unknown discount code divides by zero",
        target_symbol="shop.cart.apply_discount",
        intended_behavior="Unknown promo code should leave total unchanged",
        test_code="""
from shop.cart import apply_discount
def test_unknown_code():
    assert apply_discount(100, "INVALID") == 100
""",
    )
    res = await classify_charge_test(charge, base_dir, head_dir)
    assert res.status == "fail_valid"
    assert res.new_code is True


@pytest.mark.asyncio
async def test_regression_detected_when_base_passes(tmp_path: Path):
    base_dir, head_dir = _setup_versions(tmp_path)
    charge = Charge(
        id="C_REG",
        title="calc_subtotal ignores item quantity",
        severity="high",
        explanation="Multi-quantity items are not multiplied by qty",
        target_symbol="shop.cart.calc_subtotal",
        intended_behavior="Subtotal should sum price * qty",
        test_code="""
from shop.cart import calc_subtotal
def test_multi_qty():
    assert calc_subtotal([{"price": 10, "qty": 3}]) == 30
""",
    )
    res = await classify_charge_test(charge, base_dir, head_dir)
    assert res.status == "fail_valid"
    assert res.new_code is False


@pytest.mark.asyncio
async def test_preexisting_failure_flagged(tmp_path: Path):
    base_dir, head_dir = _setup_versions(tmp_path)
    charge = Charge(
        id="C_PRE",
        title="buggy_legacy divides by zero",
        severity="medium",
        explanation="Pre-existing bug",
        target_symbol="shop.cart.buggy_legacy",
        intended_behavior="Should not crash",
        test_code="""
from shop.cart import buggy_legacy
def test_legacy():
    assert buggy_legacy(10) == 10
""",
    )
    res = await classify_charge_test(charge, base_dir, head_dir)
    assert res.status == "fail_preexisting"
    assert res.new_code is False


@pytest.mark.asyncio
async def test_sandbox_strips_secrets_from_env(tmp_path: Path, monkeypatch):
    base_dir, head_dir = _setup_versions(tmp_path)
    monkeypatch.setenv("GEMINI_API_KEY", "super-secret-key")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp-secret-token")

    charge = Charge(
        id="C_ENV",
        title="Check env isolation",
        severity="low",
        explanation="Secrets must not leak into sandbox",
        target_symbol="shop.cart.apply_discount",
        intended_behavior="No secrets in env",
        test_code="""
import os
def test_no_secrets():
    assert "GEMINI_API_KEY" not in os.environ
    assert "GITHUB_TOKEN" not in os.environ
    # Fail intentionally so we verify test body ran without secrets
    assert False, "Secrets were properly hidden"
""",
    )
    res = await classify_charge_test(charge, base_dir, head_dir)
    assert res.status == "fail_valid"
    assert "Secrets were properly hidden" in res.output


@pytest.mark.asyncio
async def test_after_fix_and_existing_tests(tmp_path: Path):
    _, head_dir = _setup_versions(tmp_path)
    # Add an existing test in head_dir
    (head_dir / "test_existing.py").write_text(
        "from shop.cart import apply_discount\ndef test_save20(): assert apply_discount(50, 'SAVE20') == 40.0\n",
        encoding="utf-8",
    )

    # Fix shop/cart.py
    (head_dir / "shop" / "cart.py").write_text(
        """
def calc_subtotal(items):
    return sum(i["price"] * i["qty"] for i in items)

def apply_discount(total, code):
    if code == "SAVE20":
        return total * 0.8
    return total
""".strip()
        + "\n",
        encoding="utf-8",
    )

    charge = Charge(
        id="C1",
        title="calc_subtotal ignores item quantity",
        severity="high",
        explanation="Multi-quantity items are not multiplied by qty",
        target_symbol="shop.cart.calc_subtotal",
        intended_behavior="Subtotal should sum price * qty",
        test_code="""
from shop.cart import calc_subtotal
def test_multi_qty():
    assert calc_subtotal([{"price": 10, "qty": 3}]) == 30
""",
    )
    after_res = await run_charge_test_after_fix(charge, head_dir, new_code=False)
    assert after_res.status == "pass"
    assert after_res.phase == "after_fix"

    passed, _ = await run_existing_tests(head_dir)
    assert passed is True
