from __future__ import annotations

import copy
import hashlib
import hmac
import json
import os
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from httpx import ASGITransport

from apps.api.main import app
from apps.worker.tasks import process_pull_request

WEBHOOK_SECRET = os.environ["GITHUB_WEBHOOK_SECRET"]


def _signature(body: bytes) -> str:
    digest = hmac.new(WEBHOOK_SECRET.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


@pytest.fixture(autouse=True)
def _stub_celery_delay(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Never touch Redis/Celery from an HTTP test — capture enqueued calls instead."""

    calls: list[dict[str, Any]] = []

    def fake_delay(**kwargs: Any) -> None:
        calls.append(kwargs)

    monkeypatch.setattr(process_pull_request.process_pull_request_event, "delay", fake_delay)
    return calls


@pytest.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        yield client


async def test_opened_event_is_queued(
    client: httpx.AsyncClient, fixture_loader: Any, _stub_celery_delay: list[dict[str, Any]]
) -> None:
    payload = fixture_loader("pull_request_opened.json")
    body = json.dumps(payload).encode("utf-8")

    response = await client.post(
        "/webhooks/github",
        content=body,
        headers={
            "X-Hub-Signature-256": _signature(body),
            "X-GitHub-Event": "pull_request",
            "X-GitHub-Delivery": "delivery-int-1",
            "Content-Type": "application/json",
        },
    )

    assert response.status_code == 202
    assert len(_stub_celery_delay) == 1
    assert _stub_celery_delay[0]["delivery_id"] == "delivery-int-1"
    assert _stub_celery_delay[0]["pull_request_number"] == 14


async def test_invalid_signature_is_rejected(
    client: httpx.AsyncClient, fixture_loader: Any
) -> None:
    payload = fixture_loader("pull_request_opened.json")
    body = json.dumps(payload).encode("utf-8")

    response = await client.post(
        "/webhooks/github",
        content=body,
        headers={
            "X-Hub-Signature-256": "sha256=" + "0" * 64,
            "X-GitHub-Event": "pull_request",
            "X-GitHub-Delivery": "delivery-int-2",
            "Content-Type": "application/json",
        },
    )

    assert response.status_code == 401


async def test_missing_signature_is_rejected(
    client: httpx.AsyncClient, fixture_loader: Any
) -> None:
    payload = fixture_loader("pull_request_opened.json")
    body = json.dumps(payload).encode("utf-8")

    response = await client.post(
        "/webhooks/github",
        content=body,
        headers={
            "X-GitHub-Event": "pull_request",
            "X-GitHub-Delivery": "delivery-int-3",
            "Content-Type": "application/json",
        },
    )

    assert response.status_code == 401


async def test_malformed_payload_is_rejected(
    client: httpx.AsyncClient, fixture_loader: Any
) -> None:
    payload = copy.deepcopy(fixture_loader("pull_request_opened.json"))
    del payload["pull_request"]["base"]
    body = json.dumps(payload).encode("utf-8")

    response = await client.post(
        "/webhooks/github",
        content=body,
        headers={
            "X-Hub-Signature-256": _signature(body),
            "X-GitHub-Event": "pull_request",
            "X-GitHub-Delivery": "delivery-int-4",
            "Content-Type": "application/json",
        },
    )

    assert response.status_code == 400


async def test_unsupported_event_is_ignored(
    client: httpx.AsyncClient, fixture_loader: Any, _stub_celery_delay: list[dict[str, Any]]
) -> None:
    payload = fixture_loader("pull_request_opened.json")
    body = json.dumps(payload).encode("utf-8")

    response = await client.post(
        "/webhooks/github",
        content=body,
        headers={
            "X-Hub-Signature-256": _signature(body),
            "X-GitHub-Event": "issue_comment",
            "X-GitHub-Delivery": "delivery-int-5",
            "Content-Type": "application/json",
        },
    )

    assert response.status_code == 200
    assert _stub_celery_delay == []
