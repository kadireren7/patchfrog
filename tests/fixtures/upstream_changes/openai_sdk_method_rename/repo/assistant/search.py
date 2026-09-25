from openai import OpenAI

client = OpenAI()


def vectorize(text: str) -> list[float]:
    return client.embeddings.create(model="e-1", input=text).data
