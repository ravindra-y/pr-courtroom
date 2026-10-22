import pytest
from shop.cart import cart_total


def test_single_item():
    items = [{"name": "book", "price": 20.0, "qty": 1}]
    assert cart_total(items) == 20.0


def test_multiple_items():
    items = [
        {"name": "pen", "price": 5.0, "qty": 2},
        {"name": "notebook", "price": 15.0, "qty": 1},
    ]
    assert cart_total(items) == 25.0


def test_qty_zero_item():
    items = [
        {"name": "sticker", "price": 10.0, "qty": 0},
        {"name": "mug", "price": 12.0, "qty": 1},
    ]
    assert cart_total(items) == 12.0


def test_float_prices():
    items = [
        {"name": "coffee", "price": 4.25, "qty": 2},
        {"name": "muffin", "price": 3.50, "qty": 1},
    ]
    assert cart_total(items) == pytest.approx(12.0)
