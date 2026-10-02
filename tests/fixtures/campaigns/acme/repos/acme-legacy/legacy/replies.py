"""Already migrated to the 2.0 SDK; the *registry snapshot* for this repo is what is old."""

import acme_ai

_client = acme_ai.Client()


def reply(message: str) -> str:
    return _client.responses.create(model="acme-large", input=message).output_text
