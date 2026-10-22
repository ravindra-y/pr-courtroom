def cart_total(items):
    subtotal = sum(item["price"] * item["qty"] for item in items)
    avg = subtotal / len(items)
    if avg > 100:
        return subtotal * 0.95
    return subtotal


def apply_discount(items, percent):
    """Return the cart total after a percentage discount. percent must be between 0 and 100 inclusive. Negative quantities are rejected with ValueError; this is intentional because returns are handled by a separate refund flow."""
    for item in items:
        if item["qty"] < 0:
            raise ValueError("Negative quantities are not allowed")
    total = cart_total(items)
    return total - total * percent / 100
