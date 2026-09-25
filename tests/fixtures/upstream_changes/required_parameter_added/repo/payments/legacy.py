import paylane

client = paylane.Client()


def create_pinned_charge(amount: int) -> str:
    # Already pins the API version: unaffected by it becoming required.
    return client.charges.create(amount=amount, currency="eur", api_version="2026-09-01").id
