"""Regression coverage: PatchFrog must never filter `pull_request` webhook
scheduling by branch identity.

`patchfrog.ops.orchestrator.schedule_pipeline_if_eligible` doesn't even
accept a branch name as a parameter (only `repository_ref`, `commit_sha`,
`pull_request_number`) -- structurally incapable of branch-based
filtering. This test proves the *full* path (real webhook parsing ->
real ingestion -> real scheduling call) reaches the pipeline-enqueue
step identically for every base/head branch combination and every
supported action, including source/base pairs that are neither `main`
nor the PatchFrog repository's own naming conventions. See
`docs/onboarding.md`'s "Branch scope" section.
"""

from __future__ import annotations

import copy
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from apps.worker.tasks.run_review_pipeline import run_review_pipeline_task
from patchfrog.config.settings import Settings
from patchfrog.domain.github import PullRequestEventAction, RepositoryRef
from patchfrog.github.webhooks import parse_pull_request_event
from patchfrog.ops.orchestrator import schedule_pipeline_if_eligible
from patchfrog.persistence.models.installation import InstallationModel
from patchfrog.persistence.models.repository import RepositoryModel

_GITHUB_INSTALLATION_ID = 55667788
_GITHUB_REPOSITORY_ID = 998877


def _settings() -> Settings:
    return Settings(
        DATABASE_URL="sqlite+aiosqlite:///:memory:",
        REDIS_URL="redis://localhost:6379/0",
        GITHUB_APP_ID="1",
        GITHUB_PRIVATE_KEY="-----BEGIN PRIVATE KEY-----\nx\n-----END PRIVATE KEY-----",
        GITHUB_WEBHOOK_SECRET="x",
    )


@pytest.fixture(autouse=True)
def _stub_celery_delay(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    def fake_delay(**kwargs: Any) -> None:
        calls.append(kwargs)

    monkeypatch.setattr(run_review_pipeline_task, "delay", fake_delay)
    return calls


async def _eligible_repository(session_factory: async_sessionmaker[AsyncSession]) -> None:
    async with session_factory() as session:
        session.add(
            InstallationModel(
                github_installation_id=_GITHUB_INSTALLATION_ID,
                account_login="kadireren7",
                account_type="User",
                status="active",
            )
        )
        session.add(
            RepositoryModel(
                github_repository_id=_GITHUB_REPOSITORY_ID,
                owner="kadireren7",
                name="libft",
                full_name="kadireren7/libft",
                installation_id=_GITHUB_INSTALLATION_ID,
                is_selected=True,
            )
        )
        await session.commit()


@pytest.mark.parametrize(
    ("base_branch", "head_branch", "action"),
    [
        ("main", "feature/a", "opened"),
        ("develop", "feature/b", "opened"),
        ("release/1.x", "hotfix/x", "opened"),
        ("main", "feature/ft-strdup", "reopened"),
        ("main", "feature/ft-strdup", "synchronize"),
        # Unusual-but-valid branch name: mixed separators, digits.
        ("main", "feat/foo-bar_123", "opened"),
        # Neither side is "main"/"master" and the repo isn't PatchFrog's own.
        ("staging", "chore/upgrade-deps", "opened"),
    ],
)
async def test_pipeline_is_scheduled_regardless_of_branch_names(
    base_branch: str,
    head_branch: str,
    action: str,
    session_factory: async_sessionmaker[AsyncSession],
    _stub_celery_delay: list[dict[str, Any]],
    fixture_loader: Any,
) -> None:
    await _eligible_repository(session_factory)

    payload = copy.deepcopy(fixture_loader("pull_request_opened.json"))
    payload["action"] = action
    payload["repository"]["id"] = _GITHUB_REPOSITORY_ID
    payload["repository"]["owner"]["login"] = "kadireren7"
    payload["repository"]["name"] = "libft"
    payload["repository"]["full_name"] = "kadireren7/libft"
    payload["installation"]["id"] = _GITHUB_INSTALLATION_ID
    payload["pull_request"]["base"]["ref"] = base_branch
    payload["pull_request"]["head"]["ref"] = head_branch

    event = parse_pull_request_event(
        event_name="pull_request",
        delivery_id=f"delivery-{base_branch}-{head_branch}-{action}",
        payload=payload,
    )

    assert event is not None, "webhook parser unexpectedly filtered a supported action"
    assert event.action is PullRequestEventAction(action)
    assert event.base_branch == base_branch
    assert event.head_branch == head_branch

    decision = await schedule_pipeline_if_eligible(
        session_factory,
        settings=_settings(),
        repository_ref=RepositoryRef(
            github_repository_id=event.repository.github_repository_id,
            owner=event.repository.owner,
            name=event.repository.name,
            full_name=event.repository.full_name,
            installation=event.repository.installation,
        ),
        commit_sha=event.head_sha,
        pull_request_number=event.pull_request_number,
    )

    assert decision.eligible is True, (
        f"base={base_branch!r} head={head_branch!r} action={action!r} was "
        f"unexpectedly ineligible: {decision.reason}, {decision.detail}"
    )
    assert len(_stub_celery_delay) == 1


async def test_generic_push_without_a_pull_request_is_never_a_supported_event() -> None:
    """`push` isn't a `pull_request` action at all -- parse_pull_request_event
    only ever recognizes `event_name == "pull_request"`; this is the
    structural reason PatchFrog has no push-event scheduling path, not an
    incidental gap. See docs/onboarding.md's "Branch scope" section."""

    event = parse_pull_request_event(
        event_name="push",
        delivery_id="delivery-push",
        payload={"ref": "refs/heads/feature/x", "commits": []},
    )

    assert event is None
