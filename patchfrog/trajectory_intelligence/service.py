"""Top-level Trajectory Intelligence orchestrator.

:func:`build_trajectory_intelligence_report` is the one entry point.
Called once per review run, computed alongside every other Intelligence
layer in ``_execute_and_persist`` -- but note it runs **before** this
review's own :class:`~patchfrog.persistence.models.review_memory.ReviewGenerationModel`
row exists (Phase 7's own ``finalize()`` only creates it *after* the AI
review completes and persists -- see
:mod:`patchfrog.review_memory.service`'s own module docstring). The
current, in-progress head is therefore represented as a *synthetic*
:class:`~patchfrog.trajectory_intelligence.domain.TrajectoryHead`
(``generation_id=None``), never queried from the database, and its own
changed surfaces come directly from Change Intelligence's own
already-computed ``ChangeUnit.changed_candidates`` -- never a second
query for data this run already has in memory.

**The current edge is verified, never assumed.** Persisted historical
generations chain together via each one's own already-computed
``ancestry_verified`` flag (see
:mod:`patchfrog.trajectory_intelligence.queries`'s own docstring for
why that chain is transitively safe with zero new git operations). But
the edge from the *latest persisted* generation to the *current,
in-progress* commit is a different edge entirely -- one Phase 7 itself
proves (or fails to prove) once per run, in
:meth:`patchfrog.review_memory.service.IncrementalReviewMemoryService.prepare`,
via real git plumbing (:func:`patchfrog.repository.ancestry.verify_ancestor_with_diff`).
This module never re-runs that check (that would be a second,
incompatible ancestry algorithm, and a second, wasteful git fetch) --
callers thread the *already-computed* result through as
``previous_generation_ancestry_verified``. If that edge was not proven
this run (force-push, or review memory inactive for this run), any
persisted historical lineage is discarded entirely and the current head
is analyzed alone -- fail closed, never a guessed connection across an
unproven edge.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from patchfrog.change_intelligence.domain import ChangeUnit
from patchfrog.trajectory_intelligence.domain import (
    TRAJECTORY_INTELLIGENCE_VERSION,
    TrajectoryHead,
    TrajectoryIntelligenceReport,
)
from patchfrog.trajectory_intelligence.matching import (
    derive_trajectory_events,
    derive_trajectory_signals,
)
from patchfrog.trajectory_intelligence.queries import (
    fetch_changed_surfaces_for_heads,
    fetch_trajectory_heads,
)


def _current_changed_surfaces(change_units: tuple[ChangeUnit, ...]) -> tuple[tuple[str, str], ...]:
    surfaces: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for unit in change_units:
        for candidate in unit.changed_candidates:
            if candidate.qualified_name is None:
                continue
            key = (candidate.file_path, candidate.qualified_name)
            if key in seen:
                continue
            seen.add(key)
            surfaces.append(key)
    return tuple(surfaces)


async def build_trajectory_intelligence_report(
    session: AsyncSession,
    *,
    pull_request_id: uuid.UUID | None,
    current_commit_sha: str,
    as_of: datetime,
    change_units: tuple[ChangeUnit, ...] = (),
    previous_generation_ancestry_verified: bool = False,
) -> TrajectoryIntelligenceReport:
    """``pull_request_id=None`` (a local/CLI review with no PR context)
    correctly produces an empty report -- current-PR-lineage scope has
    no meaning without a PR (spec section 3).

    ``previous_generation_ancestry_verified`` is Phase 7's own
    already-computed answer (this run's
    ``PreparedReview.plan.selection.ancestry_verified``) to "is the
    latest persisted generation's commit a real git ancestor of
    ``current_commit_sha``?" -- reused verbatim, never re-derived.
    Defaults to ``False`` (fail closed) for any caller that cannot
    supply it (e.g. review memory inactive this run)."""

    if pull_request_id is None:
        return TrajectoryIntelligenceReport(
            version=TRAJECTORY_INTELLIGENCE_VERSION, lineage_valid=False, heads_considered=(), events=(), signals=(),
        )

    historical_heads = await fetch_trajectory_heads(session, pull_request_id=pull_request_id)
    current_surfaces = _current_changed_surfaces(change_units)

    if historical_heads and historical_heads[-1].commit_sha == current_commit_sha:
        # Same-SHA retry/replay: the current review IS the latest
        # persisted head. Reuse it as-is -- never append a duplicate
        # synthetic entry, never double-count this run's own changes
        # (that exact commit's persisted review_candidates already
        # cover it).
        heads = historical_heads
        lineage_valid = True
    elif historical_heads and previous_generation_ancestry_verified:
        # The edge from the latest persisted head to this exact commit
        # was proven by Phase 7's own ancestry check for this run --
        # safe to combine.
        current_head = TrajectoryHead(
            generation_id=None, review_run_id=None, commit_sha=current_commit_sha,
            sequence_number=historical_heads[-1].sequence_number + 1, observed_at=as_of.isoformat(),
        )
        heads = (*historical_heads, current_head)
        lineage_valid = True
    else:
        # Fail closed: either a fresh PR (no historical heads exist at
        # all) or the current edge was never proven this run
        # (force-push, or review memory inactive) -- any historical
        # lineage is discarded entirely; the current head is analyzed
        # alone.
        current_head = TrajectoryHead(
            generation_id=None, review_run_id=None, commit_sha=current_commit_sha,
            sequence_number=1, observed_at=as_of.isoformat(),
        )
        heads = (current_head,)
        lineage_valid = False

    review_run_ids = tuple(h.review_run_id for h in heads if h.review_run_id is not None)
    historical_surfaces = await fetch_changed_surfaces_for_heads(session, review_run_ids=review_run_ids)

    events = derive_trajectory_events(
        heads=heads, historical_surfaces_by_review_run=historical_surfaces, current_changed_surfaces=current_surfaces,
    )
    signals = derive_trajectory_signals(events)

    return TrajectoryIntelligenceReport(
        version=TRAJECTORY_INTELLIGENCE_VERSION, lineage_valid=lineage_valid, heads_considered=heads,
        events=events, signals=signals,
    )
