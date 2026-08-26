"""Read/administrative queries for ``patchfrog ops`` -- stale-run
detection, usage accounting, installation listing. Never used by the
production pipeline itself (see :mod:`patchfrog.ops.eligibility` for
that); this module exists purely to give a human operator visibility and
recovery tools.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from patchfrog.persistence.models.installation import InstallationModel
from patchfrog.persistence.models.repository import RepositoryModel
from patchfrog.persistence.models.review import ReviewRunModel
from patchfrog.review.domain import ReviewRunStatus


@dataclass(frozen=True, slots=True)
class StaleRun:
    run_id: UUID
    repository_id: UUID
    commit_sha: str
    started_at: datetime
    minutes_running: float


async def list_stale_runs(session: AsyncSession, *, threshold_minutes: int) -> list[StaleRun]:
    """A review run still ``RUNNING`` after ``threshold_minutes`` --
    almost always a crashed worker or a lost task, never expected in
    normal operation (spec section 31)."""

    cutoff = datetime.now(UTC) - timedelta(minutes=threshold_minutes)
    rows = (
        await session.execute(
            select(ReviewRunModel).where(
                ReviewRunModel.status == ReviewRunStatus.RUNNING, ReviewRunModel.started_at < cutoff
            )
        )
    ).scalars().all()
    now = datetime.now(UTC)
    return [
        StaleRun(
            run_id=r.id,
            repository_id=r.repository_id,
            commit_sha=r.commit_sha,
            started_at=r.started_at,
            minutes_running=(now - r.started_at).total_seconds() / 60,
        )
        for r in rows
    ]


@dataclass(frozen=True, slots=True)
class FailedRun:
    run_id: UUID
    repository_id: UUID
    commit_sha: str
    error_message: str | None
    completed_at: datetime | None


async def list_failed_runs(session: AsyncSession, *, since: datetime | None = None) -> list[FailedRun]:
    stmt = select(ReviewRunModel).where(ReviewRunModel.status == ReviewRunStatus.FAILED)
    if since is not None:
        stmt = stmt.where(ReviewRunModel.created_at >= since)
    rows = (await session.execute(stmt.order_by(ReviewRunModel.created_at.desc()))).scalars().all()
    return [
        FailedRun(
            run_id=r.id,
            repository_id=r.repository_id,
            commit_sha=r.commit_sha,
            error_message=r.error_message,
            completed_at=r.completed_at,
        )
        for r in rows
    ]


@dataclass(frozen=True, slots=True)
class InstallationUsage:
    github_installation_id: int
    account_login: str
    status: str
    beta_state: str
    publication_allowed: bool
    reviews_last_24h: int
    daily_limit: int


async def list_installation_usage(
    session: AsyncSession, *, default_daily_limit: int
) -> list[InstallationUsage]:
    installations = (await session.execute(select(InstallationModel))).scalars().all()
    since = datetime.now(UTC) - timedelta(hours=24)

    usage: list[InstallationUsage] = []
    for installation in installations:
        count = (
            await session.execute(
                select(func.count(ReviewRunModel.id))
                .select_from(ReviewRunModel)
                .join(RepositoryModel, RepositoryModel.id == ReviewRunModel.repository_id)
                .where(
                    RepositoryModel.installation_id == installation.github_installation_id,
                    ReviewRunModel.created_at >= since,
                )
            )
        ).scalar_one()
        usage.append(
            InstallationUsage(
                github_installation_id=installation.github_installation_id,
                account_login=installation.account_login,
                status=installation.status.value,
                beta_state=installation.beta_state.value,
                publication_allowed=installation.publication_allowed,
                reviews_last_24h=count,
                daily_limit=(
                    installation.daily_review_limit
                    if installation.daily_review_limit is not None
                    else default_daily_limit
                ),
            )
        )
    return usage
