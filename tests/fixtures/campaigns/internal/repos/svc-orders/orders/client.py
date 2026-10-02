import acme_shared

_client = acme_shared.Client()


def load(record_id: str) -> str:
    return _client.records.fetch(record_id=record_id).payload
