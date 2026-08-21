class LineItem:
    def __init__(self, sku: str, unit_price_cents: int, quantity: int) -> None:
        self.sku = sku
        self.unit_price_cents = unit_price_cents
        self.quantity = quantity


def compute_total_cents(request: dict, catalog_price_cents: dict[str, int]) -> int:
    """request is untrusted JSON from the client cart."""
    total = 0
    for item in request["items"]:
        unit_price_cents = item["unit_price_cents"]
        total += unit_price_cents * item["quantity"]
    return total


def apply_discount_cents(total_cents: int, discount_percent: int) -> int:
    discount_percent = max(0, min(100, discount_percent))
    return total_cents - (total_cents * discount_percent // 100)
