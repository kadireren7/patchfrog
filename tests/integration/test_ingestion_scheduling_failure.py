"""Regression test: a transient failure in `schedule_pipeline_if_eligible`
(e.g. a momentary Redis blip on `.delay()`) must never propagate out of
`_ingest` -- ingestion's delivery_id uniqueness constraint means a
Celery-level retry of the whole task would see the already-SUCCEEDED
ingestion as a DUPLICATE and never re-attempt scheduling, so an
unhandled exception here would silently leave a successfully-ingested
PR that never gets reviewed, invisible to both `ops failed` and
`ops stale` (neither has a `review_runs` row to look at, since the
pipeline was never scheduled in the first place). Found via adversarial
review of the ingestion -> scheduling hand-off, not via a live failure.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from apps.worker.tasks import process_pull_request
from patchfrog.config.settings import Settings
from patchfrog.domain.github import (
    InstallationRef,
    PullRequestEventAction,
    PullRequestWebhookEvent,
    RepositoryRef,
)
from patchfrog.domain.pull_request import ChangedFile, FileChangeStatus, PullRequestMetadata
from patchfrog.github.client import GitHubClient
from patchfrog.ops import metrics
from patchfrog.persistence.models import Base

_EVENT = PullRequestWebhookEvent(
    delivery_id="delivery-scheduling-failure",
    action=PullRequestEventAction.OPENED,
    repository=RepositoryRef(
        github_repository_id=112233,
        owner="kadireren7",
        name="libft",
        full_name="kadireren7/libft",
        installation=InstallationRef(id=55667788),
    ),
    pull_request_number=1,
    pull_request_title="t",
    pull_request_body=None,
    author="kadireren7",
    base_branch="main",
    head_branch="feature",
    base_sha="a" * 40,
    head_sha="b" * 40,
    html_url="https://github.com/kadireren7/libft/pull/1",
)

_PR_METADATA = PullRequestMetadata(
    number=1, title="t", body=None, author="kadireren7", base_branch="main", head_branch="feature",
    base_sha="a" * 40, head_sha="b" * 40, html_url="https://github.com/kadireren7/libft/pull/1",
    state="open", merged=False,
)


def _settings(database_url: str) -> Settings:
    base: dict[str, Any] = {
        "DATABASE_URL": database_url,
        "REDIS_URL": "redis://localhost:6379/0",
        "GITHUB_APP_ID": "1",
        "GITHUB_PRIVATE_KEY": "-----BEGIN PRIVATE KEY-----\nx\n-----END PRIVATE KEY-----",
        "GITHUB_WEBHOOK_SECRET": "x",
    }
    return Settings(**base)


async def _fake_get_pull_request(self: GitHubClient, *, installation_id: int, ref: Any) -> PullRequestMetadata:
    return _PR_METADATA


async def _fake_list_pull_request_files(self: GitHubClient, *, installation_id: int, ref: Any) -> list[ChangedFile]:
    return [
        ChangedFile(
            path="a.py", status=FileChangeStatus.MODIFIED, additions=1, deletions=0,
            patch="@@ -1,1 +1,1 @@\n-old\n+new", previous_path=None,
        )
    ]


async def _raising_schedule_pipeline_if_eligible(*args: Any, **kwargs: Any) -> None:
    raise ConnectionError("simulated Redis outage on .delay()")


async def test_scheduling_failure_after_successful_ingestion_never_propagates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "scheduling_failure.db"
    database_url = f"sqlite+aiosqlite:///{db_path}"

    setup_engine = create_async_engine(database_url)
    async with setup_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await setup_engine.dispose()

    monkeypatch.setattr(GitHubClient, "get_pull_request", _fake_get_pull_request)
    monkeypatch.setattr(GitHubClient, "list_pull_request_files", _fake_list_pull_request_files)
    monkeypatch.setattr(
        process_pull_request, "schedule_pipeline_if_eligible", _raising_schedule_pipeline_if_eligible
    )

    skipped_before = metrics.reviews_skipped_total.labels(reason="scheduling_failed")._value.get()

    outcome = await process_pull_request._ingest(_EVENT, _settings(database_url))

    assert outcome.status.value == "succeeded"
    skipped_after = metrics.reviews_skipped_total.labels(reason="scheduling_failed")._value.get()
    assert skipped_after == skipped_before + 1
