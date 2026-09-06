"""Bounded, indexed DB reads this milestone needs -- both functions
read Phase 5's/the pull-request ingestion pipeline's own already
-persisted tables directly. **No new table, zero new GitHub API calls,
zero new history crawler.** See
``validation/cross_pr_intelligence/latest-summary.md`` sections 1-6 and
19 for the full audit.

:func:`fetch_cross_pr_peers` is **one bounded SQL query** -- eligibility
(same repository, not the current PR, ``state == "open"``, latest
reviewed generation's ``commit_sha`` exactly matches the peer's current
``head_sha``) is expressed entirely in the query's own ``WHERE``
clause, and ``ORDER BY``/``LIMIT`` are applied before any row ever
reaches Python. There is no Python-side loop that inspects an
unbounded number of candidate PRs before finding
:data:`~patchfrog.cross_pr_intelligence.domain.MAX_CROSS_PR_PEERS`
eligible ones -- an external review of the original v1 shape (which
issued one unbounded ``SELECT`` for every open PR in the repository,
then a separate per-PR generation query in a Python loop until enough
eligible peers were found) found this violated "bounded repository
-scoped peer discovery"; this is the fix.

"Latest reviewed generation" is computed via the standard, portable
greatest-N-per-group pattern: a subquery aggregates
``MAX(sequence_number)`` grouped by ``pull_request_id`` (using the
existing unique index on exactly those two columns,
``uq_review_generations_pr_sequence``), then joins back to
:class:`~patchfrog.persistence.models.review_memory.ReviewGenerationModel`
on that exact ``(pull_request_id, sequence_number)`` pair. This is
deliberately never "any generation whose ``commit_sha`` happens to
equal ``head_sha``" -- an older generation sharing a peer's *current*
head_sha by coincidence (e.g. after a revert) must never be selected
over a real, later generation that superseded it. See
``test_case_latest_generation_authority_never_falls_back_to_older_match``
in the integration corpus.

Deliberately does **not** consult ``ancestry_verified`` at all (unlike
Trajectory Intelligence's own lineage walk) -- Cross-PR Intelligence
cares only about a peer's *exact current* reviewed head, never its
historical lineage. A peer whose latest generation has
``ancestry_verified=False`` (e.g. because *that peer's own* review
history included a force-push) is still eligible as long as its
``commit_sha`` matches the peer's current ``head_sha`` exactly -- see
``test_case_peer_with_unverified_ancestry_still_eligible``.

:func:`fetch_changed_surfaces_for_peers` is one bounded ``IN`` query
across every eligible peer's one latest ``review_run_id``, reading
``review_candidates`` rows with ``reason == CHANGED_SYMBOL`` and a
non-``None`` ``qualified_name`` -- never a per-peer query loop.
Deduplicates by ``(file_path, qualified_name)`` per review run before
counting against
:data:`~patchfrog.cross_pr_intelligence.domain.MAX_CROSS_PR_SURFACES_PER_PEER`,
so a duplicate row (rare, but not structurally impossible) never
consumes two surface-budget slots for what is logically one surface.
"""

from __future__ import annotations

import uuid

from sqlalchemy import func, select
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
    ``CURRENT_TIMESTAMP`` resolution is whole seconds), with ``LIMIT``
    applied in the same query -- never a query over every PR a
    repository has ever seen, and never a Python-side loop over an
    unbounded candidate set. Empty when no eligible peer exists --
    never a crash, never a guess."""

    latest_generation = (
        select(
            ReviewGenerationModel.pull_request_id.label("pull_request_id"),
            func.max(ReviewGenerationModel.sequence_number).label("max_sequence_number"),
        )
        .group_by(ReviewGenerationModel.pull_request_id)
        .subquery()
    )

    stmt = (
        select(
            PullRequestModel.id.label("pull_request_id"),
            PullRequestModel.github_pr_number.label("github_pr_number"),
            ReviewGenerationModel.commit_sha.label("commit_sha"),
            ReviewGenerationModel.review_run_id.label("review_run_id"),
            ReviewGenerationModel.sequence_number.label("sequence_number"),
        )
        .join(latest_generation, latest_generation.c.pull_request_id == PullRequestModel.id)
        .join(
            ReviewGenerationModel,
            (ReviewGenerationModel.pull_request_id == latest_generation.c.pull_request_id)
            & (ReviewGenerationModel.sequence_number == latest_generation.c.max_sequence_number),
        )
        .where(
            PullRequestModel.repository_id == repository_id,
            PullRequestModel.id != current_pull_request_id,
            PullRequestModel.state == "open",
            ReviewGenerationModel.commit_sha == PullRequestModel.head_sha,
        )
        .order_by(PullRequestModel.updated_at.desc(), PullRequestModel.github_pr_number.desc())
        .limit(limit)
    )

    result = await session.execute(stmt)
    return tuple(
        CrossPRPeer(
            pull_request_id=row.pull_request_id,
            github_pr_number=row.github_pr_number,
            head_commit_sha=row.commit_sha,
            review_run_id=row.review_run_id,
            sequence_number=row.sequence_number,
        )
        for row in result.all()
    )


async def fetch_changed_surfaces_for_peers(
    session: AsyncSession, *, review_run_ids: tuple[uuid.UUID, ...]
) -> dict[uuid.UUID, tuple[tuple[str, str], ...]]:
    """``{review_run_id: ((file_path, qualified_name), ...)}`` for every
    ``CHANGED_SYMBOL`` candidate with a known ``qualified_name`` in the
    given (already-bounded) set of peer review runs. A module-region
    candidate (``qualified_name is None``) never participates -- same
    exclusion every prior Intelligence package's own audit already
    established. Each peer's own *distinct* surface list is bounded to
    :data:`~patchfrog.cross_pr_intelligence.domain.MAX_CROSS_PR_SURFACES_PER_PEER`
    -- a duplicate ``(file_path, qualified_name)`` row for the same
    review run is deduplicated before it can consume a second slot."""

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
    seen_by_run: dict[uuid.UUID, set[tuple[str, str]]] = {}
    for review_run_id, file_path, qualified_name in result.all():
        key = (file_path, qualified_name)
        seen = seen_by_run.setdefault(review_run_id, set())
        if key in seen:
            continue  # duplicate row for an already-counted surface -- never a second slot
        surfaces = by_run.setdefault(review_run_id, [])
        if len(surfaces) >= MAX_CROSS_PR_SURFACES_PER_PEER:
            continue
        seen.add(key)
        surfaces.append(key)
    return {run_id: tuple(surfaces) for run_id, surfaces in by_run.items()}
