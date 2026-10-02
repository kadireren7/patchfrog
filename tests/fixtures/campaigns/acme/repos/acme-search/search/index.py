"""Search indexing. Only uses embeddings, which the upgrade does not change."""

import acme_ai

_client = acme_ai.Client()


def embed(text: str) -> str:
    return _client.embeddings.create(model="acme-embed", input=text).id
