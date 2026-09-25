"""Embeddings are unaffected by the 2.0 change."""

import acme_ai

client = acme_ai.Client()


def embed(text: str) -> list[float]:
    return client.embeddings.create(model="acme-embed", input=text).vector
