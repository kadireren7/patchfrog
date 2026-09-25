"""Background summarization."""

import openai


def summarize(document: str) -> str:
    client = openai.OpenAI()
    result = client.responses.create(model="gpt-4o-mini", input=f"Summarize: {document}")
    return result.output_text
