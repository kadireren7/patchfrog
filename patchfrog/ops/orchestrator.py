"""Pipeline orchestration -- decides *whether* to schedule PatchFrog's
index/analyze/review/publish chain for a pull request, and kicks off the
first stage. Never redesigns any stage's own internals (each remains a
separately invokable, separately retriable Celery task, exactly as
before) -- this module is purely an additive gate + scheduler sitting in
front of them.

Before this module existed, nothing in PatchFrog automatically chained
these stages together -- every Celery task doc comment said as much
("Nothing here triggers automatically on a webhook yet ... invoked
explicitly"). That was a deliberate, documented stopping point for
earlier phases; closing it -- so "install the App, open a PR, get a
review" actually works without a human manually invoking each stage -- is
the single highest-value change in the public-beta-readiness work (spec
section 11).
"""

from __future__ import annotations

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from patchfrog.config.settings import Settings
from patchfrog.domain.github import RepositoryRef
from patchfrog.ops.eligibility import EligibilityDecision, check_eligibility
from patchfrog.persistence.repositories import RepositoryRepository

logger = structlog.get_logger(__name__)


async def schedule_pipeline_if_eligible(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    settings: Settings,
    repository_ref: RepositoryRef,
    commit_sha: str,
    pull_request_number: int,
) -> EligibilityDecision:
    """Called once, right after a `pull_request` webhook event is
    successfully ingested. Decides whether to kick off the
    index -> analyze -> review -> publish chain at all -- checked exactly
    once, here, never re-checked at every later stage (a short-lived
    beta window doesn't need per-stage re-validation for the ordinary
    case; the one stage-specific check that *is* worth re-doing is the
    review stage's own supersession check, since queue delay specifically
    invalidates a stale commit -- see
    :mod:`apps.worker.tasks.review_pull_request`). Always returns the
    decision, even when ineligible, so the caller can log a clear reason
    rather than silently drop the PR.
    """

    async with session_factory() as session:
        repository = await RepositoryRepository().get_by_github_id(
            session, github_repository_id=repository_ref.github_repository_id
        )
        if repository is None:
            return EligibilityDecision(
                eligible=False, reason=None, detail="repository row not found immediately after ingestion"
            )
        decision = await check_eligibility(
            session,
            settings=settings,
            repository=repository,
            github_installation_id=repository_ref.installation.id,
        )

    if not decision.eligible:
        logger.info(
            "pull_request_ineligible",
            repository=repository_ref.full_name,
            pull_request_number=pull_request_number,
            commit_sha=commit_sha,
            reason=decision.reason.value if decision.reason else "unknown",
            detail=decision.detail,
        )
        return decision

    # Imported here, not at module level -- patchfrog/ never imports from
    # apps/ at import time (apps/ imports from patchfrog/, never the
    # other way around); this function is the one deliberate exception,
    # so the dependency is scoped to exactly where it's needed.
    from apps.worker.tasks.run_review_pipeline import run_review_pipeline_task

    run_review_pipeline_task.delay(
        github_repository_id=repository_ref.github_repository_id,
        owner=repository_ref.owner,
        name=repository_ref.name,
        full_name=repository_ref.full_name,
        installation_id=repository_ref.installation.id,
        commit_sha=commit_sha,
        pull_request_number=pull_request_number,
    )
    logger.info(
        "pull_request_pipeline_scheduled",
        repository=repository_ref.full_name,
        pull_request_number=pull_request_number,
        commit_sha=commit_sha,
    )
    return decision
