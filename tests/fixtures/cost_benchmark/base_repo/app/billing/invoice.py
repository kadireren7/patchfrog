"""Invoice assembly."""

from app.billing.calc import subtotal, with_tax


def build_lines(items):
    """Render one printable line per item."""
    lines = []
    for index in range(len(items)):
        item = items[index]
        lines.append(f"{item['name']} x{item['qty']}")
    return lines


def build_invoice(customer, items):
    """Assemble an invoice dict for a customer."""
    net = subtotal(items)
    return {
        "customer": customer,
        "lines": build_lines(items),
        "net": net,
        "gross": with_tax(net),
    }
