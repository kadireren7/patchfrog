"""Integration coverage for :func:`patchfrog.ops.eligibility.check_eligibility`
-- every branch needs a real session (installation/repository/quota
lookups), unlike the pure `check_resource_limits` covered in
tests/unit/test_ops_eligibility.py.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from patchfrog.config.settings import Settings
from patchfrog.ops.eligibility import IneligibilityReason, check_eligibility
from patchfrog.persistence.models.installation import (
    BetaState,
    InstallationModel,
    InstallationStatus,
)
from patchfrog.persistence.models.repository import RepositoryModel
from patchfrog.persistence.models.repository_index import IndexStatus, RepositoryIndexModel
from patchfrog.persistence.models.review import ReviewRunModel
from patchfrog.review.domain import ReviewRunStatus

_GITHUB_INSTALLATION_ID = 55667788


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


async def _make_repository(
    session: AsyncSession, *, installation_id: int = _GITHUB_INSTALLATION_ID, is_selected: bool = True
) -> RepositoryModel:
    repo = RepositoryModel(
        github_repository_id=uuid.uuid4().int & 0x7FFFFFFF,
        owner="kadireren7",
        name="libft",
        full_name="kadireren7/libft",
        installation_id=installation_id,
        is_selected=is_selected,
    )
    session.add(repo)
    await session.flush()
    return repo


async def _make_installation(
    session: AsyncSession,
    *,
    github_installation_id: int = _GITHUB_INSTALLATION_ID,
    status: InstallationStatus = InstallationStatus.ACTIVE,
    beta_state: BetaState = BetaState.ACTIVE,
    daily_review_limit: int | None = None,
) -> InstallationModel:
    installation = InstallationModel(
        github_installation_id=github_installation_id,
        account_login="kadireren7",
        account_type="User",
        status=status,
        beta_state=beta_state,
        daily_review_limit=daily_review_limit,
    )
    session.add(installation)
    await session.flush()
    return installation


async def test_global_kill_switch_blocks_before_any_lookup(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        repo = await _make_repository(session)
        decision = await check_eligibility(
            session,
            settings=_settings(GLOBAL_REVIEW_PROCESSING_ENABLED=False),
            repository=repo,
            github_installation_id=_GITHUB_INSTALLATION_ID,
        )
    assert decision.eligible is False
    assert decision.reason is IneligibilityReason.GLOBAL_PROCESSING_DISABLED


async def test_installation_mismatch_fails_closed(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        repo = await _make_repository(session, installation_id=1)
        decision = await check_eligibility(
            session, settings=_settings(), repository=repo, github_installation_id=2
        )
    assert decision.eligible is False
    assert decision.reason is IneligibilityReason.REPOSITORY_INSTALLATION_MISMATCH


async def test_repository_not_selected_blocks(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        repo = await _make_repository(session, is_selected=False)
        decision = await check_eligibility(
            session,
            settings=_settings(),
            repository=repo,
            github_installation_id=_GITHUB_INSTALLATION_ID,
        )
    assert decision.eligible is False
    assert decision.reason is IneligibilityReason.REPOSITORY_NOT_SELECTED


async def test_installation_not_found_blocks(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        repo = await _make_repository(session)
        decision = await check_eligibility(
            session,
            settings=_settings(),
            repository=repo,
            github_installation_id=_GITHUB_INSTALLATION_ID,
        )
    assert decision.eligible is False
    assert decision.reason is IneligibilityReason.INSTALLATION_NOT_FOUND


async def test_suspended_installation_blocks(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        repo = await _make_repository(session)
        await _make_installation(session, status=InstallationStatus.SUSPENDED)
        decision = await check_eligibility(
            session,
            settings=_settings(),
            repository=repo,
            github_installation_id=_GITHUB_INSTALLATION_ID,
        )
    assert decision.eligible is False
    assert decision.reason is IneligibilityReason.INSTALLATION_NOT_ACTIVE


async def test_beta_pending_blocks(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        repo = await _make_repository(session)
        await _make_installation(session, beta_state=BetaState.PENDING)
        decision = await check_eligibility(
            session,
            settings=_settings(),
            repository=repo,
            github_installation_id=_GITHUB_INSTALLATION_ID,
        )
    assert decision.eligible is False
    assert decision.reason is IneligibilityReason.BETA_NOT_ACTIVE


async def test_fully_eligible_installation_and_repository_passes(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        repo = await _make_repository(session)
        await _make_installation(session)
        decision = await check_eligibility(
            session,
            settings=_settings(),
            repository=repo,
            github_installation_id=_GITHUB_INSTALLATION_ID,
        )
    assert decision.eligible is True
    assert decision.reason is None
    assert decision.installation is not None


async def test_quota_exceeded_blocks(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        repo = await _make_repository(session)
        await _make_installation(session, daily_review_limit=1)

        index = RepositoryIndexModel(
            repository_id=repo.id, commit_sha="a" * 40, index_version=1,
            status=IndexStatus.SUCCEEDED, is_active=True, started_at=datetime.now(UTC),
        )
        session.add(index)
        await session.flush()

        run = ReviewRunModel(
            repository_id=repo.id, repository_index_id=index.id, pull_request_id=None,
            commit_sha="b" * 40, config_fingerprint="cf", model_fingerprint="mf",
            incremental_context_fingerprint="none", status=ReviewRunStatus.SUCCEEDED,
            reviewer_provider="fake", reviewer_model="fake-model", started_at=datetime.now(UTC),
        )
        session.add(run)
        await session.flush()

        decision = await check_eligibility(
            session,
            settings=_settings(),
            repository=repo,
            github_installation_id=_GITHUB_INSTALLATION_ID,
        )
    assert decision.eligible is False
    assert decision.reason is IneligibilityReason.QUOTA_EXCEEDED
