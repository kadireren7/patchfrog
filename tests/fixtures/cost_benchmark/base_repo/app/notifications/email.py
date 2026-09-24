"""Outbound notifications."""


def invoice_subject(invoice):
    """Subject line for an invoice email."""
    return f"Invoice for {invoice['customer']}"


def render_body(invoice):
    """Plain-text body for an invoice email."""
    return f"Total due: {invoice['gross']}"
