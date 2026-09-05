"""The two bounded, indexed SQL queries this milestone needs -- both
read Phase 5/Phase 7's own already-persisted tables directly. **No new
table, no new history database, zero new git operations, zero new
GitHub API calls.** See
``validation/trajectory_intelligence/latest-summary.md`` sections 1-5
for the full audit.

**Query 1** (:func:`fetch_trajectory_heads`): walks
``review_generations`` backward from the current PR's latest existing
generation via ``previous_generation_id``, reusing each generation's
own already-computed ``ancestry_verified`` flag (Phase 7's own real
git-plumbing force-push detection, run once when that generation was
created) -- stopping the moment a step's ``ancestry_verified`` is
``False`` or the chain ends. Bounded to
:data:`~patchfrog.trajectory_intelligence.domain.MAX_TRAJECTORY_HEADS`
raw steps; never an unbounded walk.

**Query 2** (:func:`fetch_changed_surfaces_for_heads`): one bounded
``IN`` query across every valid head's ``review_run_id``, reading
``review_candidates`` rows with ``reason == CHANGED_SYMBOL`` and a
non-``None`` ``qualified_name`` -- never a per-head query loop.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from patchfrog.persistence.models.review import ReviewCandidateModel
from patchfrog.persistence.models.review_memory import ReviewGenerationModel
from patchfrog.review.domain import ReviewCandidateReason
from patchfrog.trajectory_intelligence.domain import MAX_TRAJECTORY_HEADS, TrajectoryHead


async def fetch_trajectory_heads(
    session: AsyncSession, *, pull_request_id: uuid.UUID, limit: int = MAX_TRAJECTORY_HEADS
) -> tuple[TrajectoryHead, ...]:
    """Returns the current PR's verified lineage, oldest-first,
    deduplicated by ``commit_sha`` (two generations sharing an exact
    commit -- e.g. a legitimate retry that created a second
    ``review_runs`` row for the same head -- collapse to one head; spec
    section 26/latest-summary section 2). Empty when no generation
    exists yet (a PR's first review) -- never a crash, never a guess."""

    result = await session.execute(
        select(ReviewGenerationModel)
        .where(ReviewGenerationModel.pull_request_id == pull_request_id)
        .order_by(ReviewGenerationModel.sequence_number.desc())
        .limit(1)
    )
    current = result.scalar_one_or_none()
    if current is None:
        return ()

    chain: list[ReviewGenerationModel] = []
    while current is not None and len(chain) < limit:
        chain.append(current)
        if not current.ancestry_verified:
            break
        if current.previous_generation_id is None:
            break
        current = await session.get(ReviewGenerationModel, current.previous_generation_id)

    chain.reverse()  # oldest-first

    seen_commits: set[str] = set()
    heads: list[TrajectoryHead] = []
    for gen in chain:
        if gen.commit_sha in seen_commits:
            continue
        seen_commits.add(gen.commit_sha)
        heads.append(
            TrajectoryHead(
                generation_id=gen.id, review_run_id=gen.review_run_id, commit_sha=gen.commit_sha,
                sequence_number=gen.sequence_number, observed_at=gen.created_at.isoformat(),
            )
        )
    return tuple(heads)


async def fetch_changed_surfaces_for_heads(
    session: AsyncSession, *, review_run_ids: tuple[uuid.UUID, ...]
) -> dict[uuid.UUID, tuple[tuple[str, str], ...]]:
    """``{review_run_id: ((file_path, qualified_name), ...)}`` for every
    ``CHANGED_SYMBOL`` candidate with a known ``qualified_name`` in the
    given (already-bounded) set of review runs. A module-region
    candidate (``qualified_name is None``) never participates -- no
    stable non-file identity to track continuity against, the same
    exclusion N's and O's own audits already established."""

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
        by_run.setdefault(review_run_id, []).append((file_path, qualified_name))
    return {run_id: tuple(surfaces) for run_id, surfaces in by_run.items()}
