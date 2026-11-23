"""Docs mention @app.get("/docstring") but that is text."""

from unittest import mock

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient

app = FastAPI()


class Registry:
    def get(self, path):
        def wrap(fn):
            return fn

        return wrap


registry = Registry()


@registry.get("/not-a-route")
def registered():
    return None


@app.get("/real")
def real():
    return {}


def fetch():
    return httpx.get("https://example.invalid/data")


client = TestClient(app)
response = client.get("/real")
data = {"a": 1}.get("a")


@mock.patch("main.fetch")
def test_fetch(mocked):
    return mocked
