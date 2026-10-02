"""Chat replies for the support widget."""

import acme_ai

client = acme_ai.Client()


def generate_reply(message: str) -> str:
    result = client.chat.create(model="acme-large", prompt=message, temperature=0.2)
    return result.text
