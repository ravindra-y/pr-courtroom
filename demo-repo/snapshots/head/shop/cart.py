"""Shopping cart subtotal and discount calculations (HEAD version with PR #42 changes)."""


def calculate_subtotal(items: list[dict]) -> float:
    """
    Calculate the cart subtotal from a list of item dicts.
    Each item dict has 'price' (float) and optional 'quantity' (int, default 1).
    Raises ValueError if any item has a negative price or quantity.
    """
    for item in items:
        if item.get("price", 0) < 0 or item.get("quantity", 1) < 0:
            raise ValueError("Item price and quantity must be non-negative")
    # Bug introduced in refactor: forgot to multiply by item.get("quantity", 1)
    return round(sum(float(item["price"]) for item in items), 2)


def apply_discount(subtotal: float, promo_code: str | None, is_vip: bool = False) -> float:
    """
    Apply a promotional discount code and optional VIP bonus to a cart subtotal.

    Supported promo codes:
    - "SAVE10": 10% off
    - "SAVE20": 20% off
    - "FLAT50": $50 flat discount

    Note: VIP members receive an additional 5% compound discount applied AFTER
    any promo code discount. Total after discount cannot be negative (minimum 0.0).
    """
    if subtotal < 0:
        raise ValueError("Subtotal cannot be negative")

    discounted = float(subtotal)
    code = (promo_code or "").strip().upper()

    if code == "SAVE10":
        discounted = discounted * 0.90
    elif code == "SAVE20":
        discounted = discounted * 0.80
    elif code == "FLAT50":
        # Bug: fails to clamp at 0.0 when subtotal < 50.0
        discounted = discounted - 50.0
    elif code != "":
        raise ValueError(f"Unknown promo code: {promo_code}")

    if is_vip:
        discounted = discounted * 0.95

    return round(discounted, 2)


def format_receipt(subtotal: float, final_total: float) -> str:
    """Format a single-line receipt summary."""
    saved = round(subtotal - final_total, 2)
    return f"Subtotal: ${subtotal:.2f} | Saved: ${saved:.2f} | Total: ${final_total:.2f}"
