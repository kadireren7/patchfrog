"""Price calculations."""

TAX_RATE = 0.2


def subtotal(items):
    """Sum item prices."""
    # Prices are integers in cents.
    return sum(item["price"] * item["qty"] for item in items)


def with_tax(amount):
    """Apply the flat tax rate."""
    return round(amount * (1 + TAX_RATE))
