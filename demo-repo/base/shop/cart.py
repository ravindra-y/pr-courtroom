def cart_total(items):
    return sum(item["price"] * item["qty"] for item in items)
