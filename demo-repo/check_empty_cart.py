import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def load_cart_module(version: str):
    spec = importlib.util.spec_from_file_location(
        f"cart_{version}", ROOT / version / "shop" / "cart.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


base_cart = load_cart_module("base")
head_cart = load_cart_module("head")

base_res = base_cart.cart_total([])
print(f"BASE cart_total([]): {base_res}")
assert base_res == 0

try:
    head_cart.cart_total([])
    raise AssertionError("Expected ZeroDivisionError on HEAD")
except ZeroDivisionError as exc:
    print(f"HEAD cart_total([]) raised ZeroDivisionError: {exc}")
