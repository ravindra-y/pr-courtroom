import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "head"))
from shop.cart import apply_discount  # noqa: E402

items = [{"name": "book", "price": 20.0, "qty": 1}]
result = apply_discount(items, 150)
print(f"HEAD apply_discount(items, 150): {result}")
assert result < 0
