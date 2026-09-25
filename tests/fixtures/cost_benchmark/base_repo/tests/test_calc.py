from app.billing.calc import subtotal, with_tax


def test_subtotal():
    assert subtotal([{"price": 100, "qty": 2}]) == 200


def test_with_tax():
    assert with_tax(100) == 120
