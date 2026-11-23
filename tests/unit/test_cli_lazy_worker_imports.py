"""Phase 1 (ADR-011): the two CLI paths that need the Celery app still work.

``patchfrog.cli`` no longer imports ``apps.worker`` at module load (see
``tests/unit/test_cli_local_startup.py``). The only two call sites that
need it import it lazily: ``ops retry`` (re-enqueues the review
pipeline task) and ``mcp serve`` (builds a ``VerifierDispatcher`` only when
the operator enabled the verifier). These tests pin both server-mode
behaviors so the lazy import cannot silently break self-hosted workflows.
"""

from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace
from typing import Any, ClassVar

import pytest

import apps.worker.tasks.run_review_pipeline as pipeline_module
from apps.worker.celery_app import celery_app
from patchfrog import cli
from patchfrog.config.settings import Settings, get_settings
from patchfrog.executable_verification.dispatch import VerifierDispatcher
from patchfrog.persistence.models.pull_request import PullRequestModel
from patchfrog.persistence.models.repository import RepositoryModel
from patchfrog.persistence.models.review import ReviewRunModel


class _FakeEngine:
    async def dispose(self) -> None:
        return None


class _FakeSession:
    def __init__(self, rows: dict[Any, Any]) -> None:
        self._rows = rows

    async def __aenter__(self) -> _FakeSession:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def get(self, model: type, _id: object) -> Any:
        return self._rows.get(model)


def test_ops_retry_enqueues_review_pipeline_through_lazy_import(monkeypatch: pytest.MonkeyPatch) -> None:
    repository = SimpleNamespace(
        github_repository_id=42, owner="acme", name="api", full_name="acme/api", installation_id=7
    )
    run = SimpleNamespace(repository_id=uuid.uuid4(), pull_request_id=uuid.uuid4(), commit_sha="c" * 40)
    pull_request = SimpleNamespace(github_pr_number=12)
    rows: dict[Any, Any] = {ReviewRunModel: run, RepositoryModel: repository, PullRequestModel: pull_request}

    monkeypatch.setattr(cli, "create_engine", lambda _url: _FakeEngine())
    monkeypatch.setattr(cli, "create_session_factory", lambda _engine: lambda: _FakeSession(rows))
    enqueued: list[dict[str, Any]] = []
    monkeypatch.setattr(
        pipeline_module.run_review_pipeline_task, "delay", lambda **kwargs: enqueued.append(kwargs)
    )

    message = asyncio.run(cli._ops_retry_async(review_run_id=uuid.uuid4()))

    assert enqueued == [
        {
            "github_repository_id": 42,
            "owner": "acme",
            "name": "api",
            "full_name": "acme/api",
            "installation_id": 7,
            "commit_sha": "c" * 40,
            "pull_request_number": 12,
        }
    ]
    assert message.startswith("re-enqueued pipeline for acme/api#12")


class _CapturingServer:
    instances: ClassVar[list[_CapturingServer]] = []

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        _CapturingServer.instances.append(self)

    async def run_stdio(self) -> None:
        return None


@pytest.mark.parametrize("verifier_enabled", [False, True])
def test_mcp_serve_builds_verifier_dispatcher_only_when_enabled(
    monkeypatch: pytest.MonkeyPatch, verifier_enabled: bool
) -> None:
    settings: Settings = get_settings().model_copy(update={"verifier_enabled": verifier_enabled})
    monkeypatch.setattr(cli, "create_engine", lambda _url: _FakeEngine())
    monkeypatch.setattr(cli, "PatchFrogMCPServer", _CapturingServer)
    _CapturingServer.instances.clear()

    asyncio.run(cli._mcp_serve(settings))

    (server,) = _CapturingServer.instances
    dispatcher = server.kwargs["verifier_dispatcher"]
    if verifier_enabled:
        assert isinstance(dispatcher, VerifierDispatcher)
        assert dispatcher._celery_app is celery_app
        assert dispatcher._wait_timeout_seconds == settings.verifier_wait_timeout_seconds
    else:
        assert dispatcher is None
