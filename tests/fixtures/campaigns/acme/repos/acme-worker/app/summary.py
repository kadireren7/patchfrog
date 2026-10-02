"""Nightly summaries. Uses a streaming flag the 2.0 SDK removed."""

import acme_ai

_client = acme_ai.Client()


def summarize(text: str) -> str:
    response = _client.chat.create(model="acme-small", prompt="Summarize: " + text, stream=False)
    return response.text
