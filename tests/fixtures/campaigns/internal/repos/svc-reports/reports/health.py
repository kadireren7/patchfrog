import acme_shared

_client = acme_shared.Client()


def is_up() -> bool:
    return _client.health.check() is None
