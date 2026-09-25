"""Only embeddings are used here. The docs mention client.chat.create(...)
but this module never calls it."""

from mylib import chat
from openai import OpenAI

client = OpenAI()
LABEL = "chat.create"  # a string, not a call


def chat_create(text: str) -> str:  # a local function that merely shares the name
    return chat.create(text)


def index(text: str) -> list[float]:
    # client.chat.create(prompt=text) was removed long ago
    return client.embeddings.create(model="e-1", input=text).data
