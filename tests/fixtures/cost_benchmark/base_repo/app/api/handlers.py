"""HTTP handlers."""

from app.billing.invoice import build_invoice


def get_invoice(request, store):
    """Return the invoice for the requested customer."""
    customer = request["customer"]
    items = store.items_for(customer)
    return {"status": 200, "body": build_invoice(customer, items)}


def health(request):
    """Liveness probe."""
    return {"status": 200, "body": "ok"}
