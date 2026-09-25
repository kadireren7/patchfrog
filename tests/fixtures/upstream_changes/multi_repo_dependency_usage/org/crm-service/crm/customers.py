import stripe


def register(email: str) -> str:
    return stripe.customers.create(email=email).id
