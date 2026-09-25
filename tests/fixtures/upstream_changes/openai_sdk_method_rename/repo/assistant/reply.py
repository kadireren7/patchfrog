from openai import OpenAI

client = OpenAI()


def answer(question: str) -> str:
    return client.chat.create(model="m-1", prompt=question).text


def answer_many(questions: list[str]) -> list[str]:
    return [answer(q) for q in questions]
