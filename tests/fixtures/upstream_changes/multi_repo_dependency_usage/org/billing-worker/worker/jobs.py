import stripe


def run(params: dict) -> str:
    # Arguments are built elsewhere -- PatchFrog cannot see whether the
    # renamed argument is among them.
    return stripe.checkout.sessions.create(**params).url
