import stripe


def start_checkout(success_url: str) -> str:
    session = stripe.checkout.sessions.create(
        mode="payment", success_url=success_url, payment_method_types=["card"]
    )
    return session.url
