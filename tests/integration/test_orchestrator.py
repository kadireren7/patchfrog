"""Integration coverage for
:func:`patchfrog.ops.orchestrator.schedule_pipeline_if_eligible` -- the
scheduler must never enqueue the pipeline for an ineligible repository,
and must enqueue it with the right arguments for an eligible one.
Never touches Redis/Celery directly -- `.delay` is stubbed, matching
`tests/integration/test_webhook_route.py`'s convention.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from apps.worker.tasks.run_review_pipeline import run_review_pipeline_task
from patchfrog.config.settings import Settings
from patchfrog.domain.github import InstallationRef, RepositoryRef
from patchfrog.ops.orchestrator import schedule_pipeline_if_eligible
from patchfrog.persistence.models.installation import InstallationModel
from patchfrog.persistence.models.repository import RepositoryModel

_GITHUB_INSTALLATION_ID = 55667788
_GITHUB_REPOSITORY_ID = 998877


def _settings(**overrides: object) -> Settings:
    base: dict[str, Any] = {
        "DATABASE_URL": "sqlite+aiosqlite:///:memory:",
        "REDIS_URL": "redis://localhost:6379/0",
        "GITHUB_APP_ID": "1",
        "GITHUB_PRIVATE_KEY": "-----BEGIN PRIVATE KEY-----\nx\n-----END PRIVATE KEY-----",
        "GITHUB_WEBHOOK_SECRET": "x",
    }
    base.update(overrides)
    return Settings(**base)


@pytest.fixture(autouse=True)
def _stub_celery_delay(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    def fake_delay(**kwargs: Any) -> None:
        calls.append(kwargs)

    monkeypatch.setattr(run_review_pipeline_task, "delay", fake_delay)
    return calls


def _repository_ref() -> RepositoryRef:
    return RepositoryRef(
        github_repository_id=_GITHUB_REPOSITORY_ID,
        owner="kadireren7",
        name="libft",
        full_name="kadireren7/libft",
        installation=InstallationRef(id=_GITHUB_INSTALLATION_ID),
    )


async def test_ineligible_repository_never_enqueues(
    session_factory: async_sessionmaker[AsyncSession],
    _stub_celery_delay: list[dict[str, Any]],
) -> None:
    async with session_factory() as session:
        repo = RepositoryModel(
            github_repository_id=_GITHUB_REPOSITORY_ID, owner="kadireren7", name="libft",
            full_name="kadireren7/libft", installation_id=_GITHUB_INSTALLATION_ID, is_selected=False,
        )
        session.add(repo)
        await session.commit()

    decision = await schedule_pipeline_if_eligible(
        session_factory,
        settings=_settings(),
        repository_ref=_repository_ref(),
        commit_sha="a" * 40,
        pull_request_number=1,
    )

    assert decision.eligible is False
    assert _stub_celery_delay == []


async def test_unknown_repository_never_enqueues(
    session_factory: async_sessionmaker[AsyncSession],
    _stub_celery_delay: list[dict[str, Any]],
) -> None:
    decision = await schedule_pipeline_if_eligible(
        session_factory,
        settings=_settings(),
        repository_ref=_repository_ref(),
        commit_sha="a" * 40,
        pull_request_number=1,
    )

    assert decision.eligible is False
    assert _stub_celery_delay == []


async def test_eligible_repository_enqueues_with_correct_arguments(
    session_factory: async_sessionmaker[AsyncSession],
    _stub_celery_delay: list[dict[str, Any]],
) -> None:
    async with session_factory() as session:
        session.add(
            RepositoryModel(
                github_repository_id=_GITHUB_REPOSITORY_ID, owner="kadireren7", name="libft",
                full_name="kadireren7/libft", installation_id=_GITHUB_INSTALLATION_ID, is_selected=True,
            )
        )
        session.add(
            InstallationModel(
                github_installation_id=_GITHUB_INSTALLATION_ID, account_login="kadireren7", account_type="User",
            )
        )
        await session.commit()

    decision = await schedule_pipeline_if_eligible(
        session_factory,
        settings=_settings(),
        repository_ref=_repository_ref(),
        commit_sha="b" * 40,
        pull_request_number=2,
    )

    assert decision.eligible is True
    assert len(_stub_celery_delay) == 1
    call = _stub_celery_delay[0]
    assert call["github_repository_id"] == _GITHUB_REPOSITORY_ID
    assert call["installation_id"] == _GITHUB_INSTALLATION_ID
    assert call["commit_sha"] == "b" * 40
    assert call["pull_request_number"] == 2
