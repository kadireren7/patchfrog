"""Pre-flight eligibility checks -- run once, before any expensive
pipeline stage (indexing/analysis/AI review) is ever scheduled for a
pull request.

Fail closed: any inconsistency in the installation/repository ownership
mapping, any disabled kill switch, any exceeded quota or resource limit
means the work is never scheduled -- never partially processed, never
guessed at (Phase "public beta readiness" spec section 10).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from patchfrog.config.settings import Settings
from patchfrog.persistence.models.installation import (
    BetaState,
    InstallationModel,
    InstallationStatus,
)
from patchfrog.persistence.models.repository import RepositoryModel
from patchfrog.persistence.models.review import ReviewRunModel
from patchfrog.persistence.repositories import InstallationRepository


class IneligibilityReason(StrEnum):
    """Every reason a PR is not scheduled for processing -- always
    recorded and logged, never a silent no-op (spec section 13: "Users
    should not see silent failure.")."""

    INSTALLATION_NOT_FOUND = "installation_not_found"
    INSTALLATION_NOT_ACTIVE = "installation_not_active"
    BETA_NOT_ACTIVE = "beta_not_active"
    REPOSITORY_NOT_SELECTED = "repository_not_selected"
    REPOSITORY_INSTALLATION_MISMATCH = "repository_installation_mismatch"
    GLOBAL_PROCESSING_DISABLED = "global_processing_disabled"
    QUOTA_EXCEEDED = "quota_exceeded"
    RESOURCE_LIMIT_EXCEEDED = "resource_limit_exceeded"


@dataclass(frozen=True, slots=True)
class EligibilityDecision:
    eligible: bool
    reason: IneligibilityReason | None = None
    detail: str = ""
    installation: InstallationModel | None = None


async def check_eligibility(
    session: AsyncSession,
    *,
    settings: Settings,
    repository: RepositoryModel,
    github_installation_id: int,
) -> EligibilityDecision:
    """The single gate every PR must pass before PatchFrog spends any
    indexing/analysis/AI-review resource on it. Checked once, right after
    ingestion -- never re-checked at every later stage (that would be
    its own source of races and complexity for negligible extra safety
    during a short-lived beta window; see the module docstring of
    :mod:`patchfrog.ops.orchestrator`)."""

    if not settings.global_review_processing_enabled:
        return EligibilityDecision(eligible=False, reason=IneligibilityReason.GLOBAL_PROCESSING_DISABLED)

    if repository.installation_id != github_installation_id:
        # A forged/misrouted event, or a repository transferred between
        # installations without PatchFrog observing the transfer -- fail
        # closed rather than trust the newer of two disagreeing sources.
        return EligibilityDecision(
            eligible=False,
            reason=IneligibilityReason.REPOSITORY_INSTALLATION_MISMATCH,
            detail=(
                f"repository installation_id={repository.installation_id} != "
                f"event installation_id={github_installation_id}"
            ),
        )

    if not repository.is_selected:
        return EligibilityDecision(eligible=False, reason=IneligibilityReason.REPOSITORY_NOT_SELECTED)

    installation = await InstallationRepository().get_by_github_id(
        session, github_installation_id=github_installation_id
    )
    if installation is None:
        return EligibilityDecision(eligible=False, reason=IneligibilityReason.INSTALLATION_NOT_FOUND)
    if installation.status is not InstallationStatus.ACTIVE:
        return EligibilityDecision(
            eligible=False, reason=IneligibilityReason.INSTALLATION_NOT_ACTIVE, installation=installation
        )
    if installation.beta_state is not BetaState.ACTIVE:
        return EligibilityDecision(
            eligible=False, reason=IneligibilityReason.BETA_NOT_ACTIVE, installation=installation
        )

    quota_ok, quota_detail = await _check_quota(session, settings=settings, installation=installation)
    if not quota_ok:
        return EligibilityDecision(
            eligible=False,
            reason=IneligibilityReason.QUOTA_EXCEEDED,
            detail=quota_detail,
            installation=installation,
        )

    return EligibilityDecision(eligible=True, installation=installation)


async def _check_quota(
    session: AsyncSession, *, settings: Settings, installation: InstallationModel
) -> tuple[bool, str]:
    limit = (
        installation.daily_review_limit
        if installation.daily_review_limit is not None
        else settings.default_daily_review_limit
    )
    since = datetime.now(UTC) - timedelta(hours=24)
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
    if count >= limit:
        return False, f"{count}/{limit} reviews in the last 24h"
    return True, ""


def check_resource_limits(
    *, settings: Settings, changed_files_count: int, diff_bytes: int
) -> tuple[bool, str]:
    """Checked once, before indexing starts (spec section 22) -- a PR
    over either limit is skipped entirely, never partially processed."""

    if changed_files_count > settings.max_changed_files:
        return False, f"{changed_files_count} changed files exceeds limit {settings.max_changed_files}"
    if diff_bytes > settings.max_diff_bytes:
        return False, f"{diff_bytes} diff bytes exceeds limit {settings.max_diff_bytes}"
    return True, ""
