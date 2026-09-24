"""Chat replies backed by the OpenAI SDK."""

from openai import OpenAI

client = OpenAI()


def generate_reply(prompt: str) -> str:
    response = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": prompt}],
    )
    return response.choices[0].message.content or ""


def moderate(text: str) -> bool:
    result = client.moderations.create(input=text)
    return bool(result.results[0].flagged)
