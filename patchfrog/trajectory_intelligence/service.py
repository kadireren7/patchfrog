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

The one bounded query this package issues
(:mod:`patchfrog.trajectory_intelligence.queries`) reuses Phase 7's
own already-persisted, already-ancestry-verified lineage. Zero LLM
calls, zero new git operations, zero new GitHub API calls.
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
) -> TrajectoryIntelligenceReport:
    """``pull_request_id=None`` (a local/CLI review with no PR context)
    correctly produces an empty report -- current-PR-lineage scope has
    no meaning without a PR (spec section 3)."""

    if pull_request_id is None:
        return TrajectoryIntelligenceReport(
            version=TRAJECTORY_INTELLIGENCE_VERSION, lineage_valid=False, heads_considered=(), events=(), signals=(),
        )

    historical_heads = await fetch_trajectory_heads(session, pull_request_id=pull_request_id)
    review_run_ids = tuple(h.review_run_id for h in historical_heads if h.review_run_id is not None)
    historical_surfaces = await fetch_changed_surfaces_for_heads(session, review_run_ids=review_run_ids)

    current_head = TrajectoryHead(
        generation_id=None, review_run_id=None, commit_sha=current_commit_sha,
        sequence_number=(historical_heads[-1].sequence_number + 1) if historical_heads else 1,
        observed_at=as_of.isoformat(),
    )

    events = derive_trajectory_events(
        historical_heads=historical_heads, historical_surfaces_by_review_run=historical_surfaces,
        current_head=current_head, current_changed_surfaces=_current_changed_surfaces(change_units),
    )
    signals = derive_trajectory_signals(events)

    return TrajectoryIntelligenceReport(
        version=TRAJECTORY_INTELLIGENCE_VERSION, lineage_valid=bool(historical_heads), events=events, signals=signals,
        heads_considered=historical_heads,
    )
