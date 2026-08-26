"""Integration coverage for the supersession re-check inside
`_review_pull_request` (apps/worker/tasks/review_pull_request.py) --
spec section 21: a commit superseded by a newer one before the AI
review stage starts must be skipped, with no review run row created and
no further GitHub/provider work attempted.

Runs against a real SQLite *file* database (not `:memory:`) because
`_review_pull_request` creates its own engine directly from
`settings.database_url`, independent of any session fixture -- a file
path lets that fresh engine see the same schema/rows a setup step wrote.
GitHub network calls are stubbed at the client-method boundary so this
test never makes a real HTTP request.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from apps.worker.tasks.review_pull_request import _review_pull_request
from patchfrog.config.settings import Settings
from patchfrog.domain.pull_request import PullRequestMetadata
from patchfrog.github.auth import InstallationTokenProvider
from patchfrog.github.client import GitHubClient
from patchfrog.ops import metrics
from patchfrog.persistence.models import Base

_GITHUB_INSTALLATION_ID = 55667788
_GITHUB_REPOSITORY_ID = 998877
_QUEUED_HEAD_SHA = "a" * 40
_CURRENT_HEAD_SHA = "b" * 40


def _settings(database_url: str) -> Settings:
    base: dict[str, Any] = {
        "DATABASE_URL": database_url,
        "REDIS_URL": "redis://localhost:6379/0",
        "GITHUB_APP_ID": "1",
        "GITHUB_PRIVATE_KEY": "-----BEGIN PRIVATE KEY-----\nx\n-----END PRIVATE KEY-----",
        "GITHUB_WEBHOOK_SECRET": "x",
    }
    return Settings(**base)


async def _fake_get_token(self: InstallationTokenProvider, installation_id: int) -> str:
    return "fake-installation-token"


async def _fake_get_pull_request(self: GitHubClient, *, installation_id: int, ref: Any) -> PullRequestMetadata:
    return PullRequestMetadata(
        number=ref.number, title="t", body=None, author="kadireren7", base_branch="main",
        head_branch="feature", base_sha="c" * 40, head_sha=_CURRENT_HEAD_SHA,
        html_url="https://github.com/kadireren7/libft/pull/1", state="open", merged=False,
    )


async def test_superseded_commit_is_skipped_before_any_provider_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "supersession.db"
    database_url = f"sqlite+aiosqlite:///{db_path}"

    setup_engine = create_async_engine(database_url)
    async with setup_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await setup_engine.dispose()

    monkeypatch.setattr(InstallationTokenProvider, "get_token", _fake_get_token)
    monkeypatch.setattr(GitHubClient, "get_pull_request", _fake_get_pull_request)

    skipped_before = metrics.reviews_skipped_total.labels(reason="superseded")._value.get()

    result = await _review_pull_request(
        github_repository_id=_GITHUB_REPOSITORY_ID,
        owner="kadireren7",
        name="libft",
        full_name="kadireren7/libft",
        installation_id=_GITHUB_INSTALLATION_ID,
        pull_request_number=1,
        head_sha=_QUEUED_HEAD_SHA,
        settings=_settings(database_url),
    )

    assert result is None
    skipped_after = metrics.reviews_skipped_total.labels(reason="superseded")._value.get()
    assert skipped_after == skipped_before + 1
