import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "head"))
from shop.cart import apply_discount  # noqa: E402

items = [{"name": "book", "price": 20.0, "qty": -1}]
try:
    apply_discount(items, 10)
    raise AssertionError("Expected ValueError for negative qty")
except ValueError as exc:
    print(f"HEAD apply_discount with negative qty raised ValueError: {exc}")
