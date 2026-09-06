"""Top-level Cross-PR Intelligence orchestrator.

:func:`build_cross_pr_intelligence_report` is the one entry point.
Called once per review run, computed alongside every other
Intelligence layer in ``_execute_and_persist``. Unlike Trajectory
Intelligence (Milestone P), this package needs no ancestry-verification
threading through the caller: peer eligibility (same repository, still
open, non-stale reviewed head) is entirely self-contained inside
:mod:`patchfrog.cross_pr_intelligence.queries`, decided fresh from
already-persisted state every call -- it never reasons about *this
run's own* lineage continuity, only about *other* PRs' independently
-tracked state. See
``validation/cross_pr_intelligence/latest-summary.md`` section 13.
"""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from patchfrog.change_intelligence.domain import ChangeUnit
from patchfrog.cross_pr_intelligence.domain import (
    CROSS_PR_INTELLIGENCE_VERSION,
    CrossPRIntelligenceReport,
)
from patchfrog.cross_pr_intelligence.matching import (
    derive_cross_pr_overlaps,
    derive_cross_pr_signals,
)
from patchfrog.cross_pr_intelligence.queries import (
    fetch_changed_surfaces_for_peers,
    fetch_cross_pr_peers,
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


async def build_cross_pr_intelligence_report(
    session: AsyncSession,
    *,
    repository_id: uuid.UUID,
    pull_request_id: uuid.UUID | None,
    change_units: tuple[ChangeUnit, ...] = (),
) -> CrossPRIntelligenceReport:
    """``pull_request_id=None`` (a local/CLI review with no PR context)
    correctly produces an empty report -- cross-PR scope has no meaning
    without a PR to compare peers against."""

    if pull_request_id is None:
        return CrossPRIntelligenceReport(
            version=CROSS_PR_INTELLIGENCE_VERSION, peers_considered=(), overlaps=(), signals=(),
        )

    peers = await fetch_cross_pr_peers(
        session, repository_id=repository_id, current_pull_request_id=pull_request_id
    )
    current_surfaces = _current_changed_surfaces(change_units)

    review_run_ids = tuple(p.review_run_id for p in peers)
    peer_surfaces = await fetch_changed_surfaces_for_peers(session, review_run_ids=review_run_ids)

    overlaps = derive_cross_pr_overlaps(
        peers=peers, peer_surfaces_by_review_run=peer_surfaces, current_changed_surfaces=current_surfaces,
    )
    signals = derive_cross_pr_signals(overlaps)

    return CrossPRIntelligenceReport(
        version=CROSS_PR_INTELLIGENCE_VERSION, peers_considered=peers, overlaps=overlaps, signals=signals,
    )
