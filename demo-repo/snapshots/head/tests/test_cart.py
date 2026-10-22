from shop.cart import calculate_subtotal, format_receipt


def test_single_item_subtotal():
    items = [{"name": "Keyboard", "price": 49.99, "quantity": 1}]
    assert calculate_subtotal(items) == 49.99


def test_format_receipt():
    line = format_receipt(100.0, 80.0)
    assert line == "Subtotal: $100.00 | Saved: $20.00 | Total: $80.00"
