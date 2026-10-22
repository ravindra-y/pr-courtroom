"""Shopping cart subtotal calculations (BASE version prior to PR #42)."""


def calculate_subtotal(items: list[dict]) -> float:
    """
    Calculate the cart subtotal from a list of item dicts.
    Each item dict has 'price' (float) and optional 'quantity' (int, default 1).
    Raises ValueError if any item has a negative price or quantity.
    """
    total = 0.0
    for item in items:
        price = float(item["price"])
        quantity = int(item.get("quantity", 1))
        if price < 0 or quantity < 0:
            raise ValueError("Item price and quantity must be non-negative")
        total += price * quantity
    return round(total, 2)


def format_receipt(subtotal: float, final_total: float) -> str:
    """Format a single-line receipt summary."""
    saved = round(subtotal - final_total, 2)
    return f"Subtotal: ${subtotal:.2f} | Saved: ${saved:.2f} | Total: ${final_total:.2f}"
