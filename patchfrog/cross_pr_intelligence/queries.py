"""Bounded, indexed DB reads this milestone needs -- both functions
read Phase 5's/the pull-request ingestion pipeline's own already
-persisted tables directly. **No new table, zero new GitHub API calls,
zero new history crawler.** See
``validation/cross_pr_intelligence/latest-summary.md`` sections 1-6 for
the full audit.

:func:`fetch_cross_pr_peers` is a **hard same-repository filter**
(``PullRequestModel.repository_id == repository_id``) followed by one
bounded per-candidate ``session.get()``-style read of each candidate's
latest :class:`~patchfrog.persistence.models.review_memory.ReviewGenerationModel`
row -- never a query over every PR a repository has ever seen.

:func:`fetch_changed_surfaces_for_peers` is one bounded ``IN`` query
across every eligible peer's one latest ``review_run_id``, reading
``review_candidates`` rows with ``reason == CHANGED_SYMBOL`` and a
non-``None`` ``qualified_name`` -- never a per-peer query loop.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from patchfrog.cross_pr_intelligence.domain import (
    MAX_CROSS_PR_PEERS,
    MAX_CROSS_PR_SURFACES_PER_PEER,
    CrossPRPeer,
)
from patchfrog.persistence.models.pull_request import PullRequestModel
from patchfrog.persistence.models.review import ReviewCandidateModel
from patchfrog.persistence.models.review_memory import ReviewGenerationModel
from patchfrog.review.domain import ReviewCandidateReason


async def _latest_generation_for_pr(
    session: AsyncSession, *, pull_request_id: uuid.UUID
) -> ReviewGenerationModel | None:
    result = await session.execute(
        select(ReviewGenerationModel)
        .where(ReviewGenerationModel.pull_request_id == pull_request_id)
        .order_by(ReviewGenerationModel.sequence_number.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def fetch_cross_pr_peers(
    session: AsyncSession,
    *,
    repository_id: uuid.UUID,
    current_pull_request_id: uuid.UUID,
    limit: int = MAX_CROSS_PR_PEERS,
) -> tuple[CrossPRPeer, ...]:
    """Returns the current PR's eligible peers: same repository, still
    open, with a real reviewed generation whose ``commit_sha`` exactly
    matches the peer's own currently-known ``head_sha`` (never stale --
    see latest-summary.md section 4). Ordered by ``updated_at``
    descending (most recently active peer first), tie-broken by
    ``github_pr_number`` descending (deterministic even when two
    updates land in the same timestamp tick -- SQLite's default
    ``CURRENT_TIMESTAMP`` resolution is whole seconds) before the
    ``limit`` cap. Empty when no eligible peer exists -- never a crash,
    never a guess."""

    result = await session.execute(
        select(PullRequestModel)
        .where(
            PullRequestModel.repository_id == repository_id,
            PullRequestModel.id != current_pull_request_id,
            PullRequestModel.state == "open",
        )
        .order_by(PullRequestModel.updated_at.desc(), PullRequestModel.github_pr_number.desc())
    )
    candidate_prs = result.scalars().all()

    peers: list[CrossPRPeer] = []
    for pr in candidate_prs:
        if len(peers) >= limit:
            break
        generation = await _latest_generation_for_pr(session, pull_request_id=pr.id)
        if generation is None:
            continue  # never reviewed -- no structural surface to compare
        if generation.commit_sha != pr.head_sha:
            continue  # stale: pr moved past its last reviewed head -- fail closed
        peers.append(
            CrossPRPeer(
                pull_request_id=pr.id,
                github_pr_number=pr.github_pr_number,
                head_commit_sha=generation.commit_sha,
                review_run_id=generation.review_run_id,
                sequence_number=generation.sequence_number,
            )
        )
    return tuple(peers)


async def fetch_changed_surfaces_for_peers(
    session: AsyncSession, *, review_run_ids: tuple[uuid.UUID, ...]
) -> dict[uuid.UUID, tuple[tuple[str, str], ...]]:
    """``{review_run_id: ((file_path, qualified_name), ...)}`` for every
    ``CHANGED_SYMBOL`` candidate with a known ``qualified_name`` in the
    given (already-bounded) set of peer review runs. A module-region
    candidate (``qualified_name is None``) never participates -- same
    exclusion every prior Intelligence package's own audit already
    established. Each peer's own surface list is bounded to
    :data:`~patchfrog.cross_pr_intelligence.domain.MAX_CROSS_PR_SURFACES_PER_PEER`."""

    if not review_run_ids:
        return {}

    result = await session.execute(
        select(ReviewCandidateModel.review_run_id, ReviewCandidateModel.file_path, ReviewCandidateModel.qualified_name)
        .where(
            ReviewCandidateModel.review_run_id.in_(review_run_ids),
            ReviewCandidateModel.reason == ReviewCandidateReason.CHANGED_SYMBOL,
            ReviewCandidateModel.qualified_name.is_not(None),
        )
    )
    by_run: dict[uuid.UUID, list[tuple[str, str]]] = {}
    for review_run_id, file_path, qualified_name in result.all():
        surfaces = by_run.setdefault(review_run_id, [])
        if len(surfaces) >= MAX_CROSS_PR_SURFACES_PER_PEER:
            continue
        surfaces.append((file_path, qualified_name))
    return {run_id: tuple(surfaces) for run_id, surfaces in by_run.items()}
