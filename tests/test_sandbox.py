import asyncio
import shutil
from pathlib import Path
import pytest

from backend.models import Charge
from backend.sandbox import (
    apply_fix,
    classify,
    classify_all_async,
    revert_fix,
    run_after_fix,
    run_existing_tests,
    symbol_exists,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
DEMO_BASE = REPO_ROOT / "demo-repo" / "base"
DEMO_HEAD = REPO_ROOT / "demo-repo" / "head"


@pytest.fixture()
def demo_dirs(tmp_path: Path) -> tuple[Path, Path, Path]:
    """Copy demo-repo/base and demo-repo/head into an isolated tmp_path workspace."""
    base_dir = tmp_path / "base"
    head_dir = tmp_path / "head"
    tests_dir = tmp_path / "tests_generated"
    shutil.copytree(DEMO_BASE, base_dir)
    shutil.copytree(DEMO_HEAD, head_dir)
    tests_dir.mkdir(parents=True, exist_ok=True)
    return head_dir, base_dir, tests_dir


def test_symbol_exists_ast(demo_dirs: tuple[Path, Path, Path]):
    head_dir, base_dir, _ = demo_dirs
    assert symbol_exists(base_dir, "shop.cart.cart_total") is True
    assert symbol_exists(base_dir, "shop.cart.apply_discount") is False
    assert symbol_exists(head_dir, "shop.cart.cart_total") is True
    assert symbol_exists(head_dir, "shop.cart.apply_discount") is True
    assert symbol_exists(base_dir, "shop.missing_mod.fn") is False


def test_case_1_empty_cart_fail_valid_modified_code(demo_dirs: tuple[Path, Path, Path]):
    """Case 1: empty cart test -> fail_valid, new_code=False (ZeroDivisionError on head, passes on base)."""
    head_dir, base_dir, tests_dir = demo_dirs
    charge = Charge(
        id="C1",
        title="Empty cart raises ZeroDivisionError",
        severity="high",
        explanation="cart_total divides by len(items) without checking for empty list",
        target_symbol="shop.cart.cart_total",
        intended_behavior="cart_total([]) should return 0",
        test_code=(
            "from shop.cart import cart_total\n\n"
            "def test_empty_cart():\n"
            "    assert cart_total([]) == 0\n"
        ),
    )
    result = classify(charge, head_dir, base_dir, tests_dir)
    assert result.status == "fail_valid"
    assert result.new_code is False
    assert result.phase == "initial"
    assert "ZeroDivisionError" in result.output
    assert str(head_dir) not in result.output


def test_case_2_discount_over_100_fail_valid_new_code(demo_dirs: tuple[Path, Path, Path]):
    """Case 2: discount over 100 test -> fail_valid, new_code=True."""
    head_dir, base_dir, tests_dir = demo_dirs
    charge = Charge(
        id="C2",
        title="apply_discount accepts percent > 100 and returns negative total",
        severity="high",
        explanation="percent is not validated to be between 0 and 100 inclusive",
        target_symbol="shop.cart.apply_discount",
        intended_behavior="percent must be between 0 and 100 inclusive",
        test_code=(
            "import pytest\n"
            "from shop.cart import apply_discount\n\n"
            "def test_discount_over_100():\n"
            "    items = [{'name': 'book', 'price': 20.0, 'qty': 1}]\n"
            "    assert apply_discount(items, 150) >= 0\n"
        ),
    )
    result = classify(charge, head_dir, base_dir, tests_dir)
    assert result.status == "fail_valid"
    assert result.new_code is True
    assert result.phase == "initial"


def test_case_3_passing_test_on_head_returns_pass(demo_dirs: tuple[Path, Path, Path]):
    """Case 3: a test that passes on head -> 'pass'."""
    head_dir, base_dir, tests_dir = demo_dirs
    charge = Charge(
        id="C3",
        title="Negative quantity raises ValueError",
        severity="low",
        explanation="Checking negative quantity handling",
        target_symbol="shop.cart.apply_discount",
        intended_behavior="Negative quantities are rejected with ValueError",
        test_code=(
            "import pytest\n"
            "from shop.cart import apply_discount\n\n"
            "def test_negative_qty_raises():\n"
            "    with pytest.raises(ValueError):\n"
            "        apply_discount([{'name': 'book', 'price': 10.0, 'qty': -1}], 10)\n"
        ),
    )
    result = classify(charge, head_dir, base_dir, tests_dir)
    assert result.status == "pass"
    assert result.new_code is False
    assert result.phase == "initial"


def test_case_4_syntax_error_and_missing_module_return_error(
    demo_dirs: tuple[Path, Path, Path]
):
    """Case 4: a test with a syntax error and a test importing a missing module -> 'error'."""
    head_dir, base_dir, tests_dir = demo_dirs
    syntax_charge = Charge(
        id="C4_syntax",
        title="Broken syntax test",
        severity="low",
        explanation="Generated test has syntax error",
        target_symbol="shop.cart.cart_total",
        intended_behavior="N/A",
        test_code="def test_broken(\n    assert True\n",
    )
    import_charge = Charge(
        id="C4_import",
        title="Missing module import test",
        severity="low",
        explanation="Imports non-existent package",
        target_symbol="shop.cart.cart_total",
        intended_behavior="N/A",
        test_code=(
            "import non_existent_module_xyz\n\n"
            "def test_missing_mod():\n"
            "    assert True\n"
        ),
    )

    res_syntax = classify(syntax_charge, head_dir, base_dir, tests_dir)
    res_import = classify(import_charge, head_dir, base_dir, tests_dir)
    assert res_syntax.status == "error"
    assert res_import.status == "error"


def test_case_5_infinite_loop_test_returns_timeout(demo_dirs: tuple[Path, Path, Path]):
    """Case 5: an infinite-loop test -> 'timeout'."""
    head_dir, base_dir, tests_dir = demo_dirs
    charge = Charge(
        id="C5",
        title="Infinite loop test",
        severity="low",
        explanation="Hangs forever",
        target_symbol="shop.cart.cart_total",
        intended_behavior="Should complete",
        test_code=(
            "def test_infinite_loop():\n"
            "    while True:\n"
            "        pass\n"
        ),
    )
    result = classify(charge, head_dir, base_dir, tests_dir, timeout=1.0)
    assert result.status == "timeout"
    assert result.new_code is False


def test_case_6_fails_on_both_head_and_base_returns_fail_preexisting(
    demo_dirs: tuple[Path, Path, Path]
):
    """Case 6: a test that fails on both head and base -> 'fail_preexisting'."""
    head_dir, base_dir, tests_dir = demo_dirs
    charge = Charge(
        id="C6",
        title="Missing 'price' key in item dict raises KeyError",
        severity="medium",
        explanation="Both base and head expect 'price' key in item dict",
        target_symbol="shop.cart.cart_total",
        intended_behavior="Missing price defaults to 0",
        test_code=(
            "from shop.cart import cart_total\n\n"
            "def test_missing_price_key():\n"
            "    assert cart_total([{'name': 'freebie', 'qty': 1}]) == 0\n"
        ),
    )
    result = classify(charge, head_dir, base_dir, tests_dir)
    assert result.status == "fail_preexisting"
    assert result.new_code is False


def test_case_7_apply_fix_correct_fix_passes_charge_and_existing_tests(
    demo_dirs: tuple[Path, Path, Path]
):
    """Case 7: apply_fix with a correct fix -> after-fix test passes and existing tests still pass."""
    head_dir, base_dir, tests_dir = demo_dirs
    charge = Charge(
        id="C1",
        title="Empty cart raises ZeroDivisionError",
        severity="high",
        explanation="cart_total divides by len(items) on empty list",
        target_symbol="shop.cart.cart_total",
        intended_behavior="cart_total([]) should return 0",
        test_code=(
            "from shop.cart import cart_total\n\n"
            "def test_empty_cart():\n"
            "    assert cart_total([]) == 0\n"
        ),
    )
    initial = classify(charge, head_dir, base_dir, tests_dir)
    assert initial.status == "fail_valid"

    fixed_code = (
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
        "    if percent < 0 or percent > 100:\n"
        "        raise ValueError('percent must be between 0 and 100 inclusive')\n"
        "    for item in items:\n"
        "        if item['qty'] < 0:\n"
        "            raise ValueError('Negative quantities are not allowed')\n"
        "    total = cart_total(items)\n"
        "    return total - total * percent / 100\n"
    )

    diff = apply_fix(head_dir, "shop/cart.py", fixed_code)
    assert "--- a/shop/cart.py" in diff
    assert "+++ b/shop/cart.py" in diff
    assert "+    if not items:" in diff

    after_res = run_after_fix(charge, head_dir, tests_dir)
    assert after_res.status == "pass"
    assert after_res.phase == "after_fix"

    existing_passed, existing_out = run_existing_tests(head_dir)
    assert existing_passed is True
    assert "4 passed" in existing_out


def test_case_8_apply_fix_bad_fix_breaks_existing_tests(
    demo_dirs: tuple[Path, Path, Path]
):
    """Case 8: apply_fix with a bad fix that breaks cart_total -> existing tests fail (regression detected)."""
    head_dir, base_dir, tests_dir = demo_dirs
    charge = Charge(
        id="C1",
        title="Empty cart raises ZeroDivisionError",
        severity="high",
        explanation="cart_total divides by len(items) on empty list",
        target_symbol="shop.cart.cart_total",
        intended_behavior="cart_total([]) should return 0",
        test_code=(
            "from shop.cart import cart_total\n\n"
            "def test_empty_cart():\n"
            "    assert cart_total([]) == 0\n"
        ),
    )

    # Bad fix: makes cart_total always return 0, which passes test_empty_cart but breaks existing tests!
    bad_code = (
        "def cart_total(items):\n"
        "    return 0\n\n\n"
        "def apply_discount(items, percent):\n"
        "    return 0\n"
    )
    apply_fix(head_dir, "shop/cart.py", bad_code)

    after_res = run_after_fix(charge, head_dir, tests_dir)
    assert after_res.status == "pass"

    existing_passed, _ = run_existing_tests(head_dir)
    assert existing_passed is False

    # Revert the bad fix and verify existing tests pass again
    revert_fix(head_dir, "shop/cart.py")
    restored_passed, _ = run_existing_tests(head_dir)
    assert restored_passed is True


def test_async_parallel_classification(demo_dirs: tuple[Path, Path, Path]):
    head_dir, base_dir, tests_dir = demo_dirs
    c1 = Charge(
        id="C1",
        title="Empty cart",
        severity="high",
        explanation="ZeroDivisionError",
        target_symbol="shop.cart.cart_total",
        intended_behavior="Return 0",
        test_code="from shop.cart import cart_total\ndef test_e(): assert cart_total([]) == 0\n",
    )
    c2 = Charge(
        id="C2",
        title="Discount > 100",
        severity="high",
        explanation="Negative total",
        target_symbol="shop.cart.apply_discount",
        intended_behavior="0..100",
        test_code=(
            "from shop.cart import apply_discount\n"
            "def test_d(): assert apply_discount([{'name': 'a', 'price': 10.0, 'qty': 1}], 150) >= 0\n"
        ),
    )
    results = asyncio.run(classify_all_async([c1, c2], head_dir, base_dir, tests_dir))
    assert len(results) == 2
    assert results[0].status == "fail_valid" and results[0].new_code is False
    assert results[1].status == "fail_valid" and results[1].new_code is True
