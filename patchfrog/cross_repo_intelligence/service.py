"""Top-level Cross-Repo Intelligence orchestrator.

:func:`build_cross_repo_intelligence_report` is the one entry point.
Reuses Contract & Blast Radius Intelligence's own already-computed
``ContractIntelligenceReport.deltas`` directly -- never a second
contract-detection engine (spec section 21). No ancestry-verification
threading needed (mirrors Cross-PR Intelligence's own reasoning, see
``validation/cross_repo_intelligence/latest-summary.md`` section 8):
peer eligibility is entirely self-contained inside
:mod:`patchfrog.cross_repo_intelligence.queries`, decided fresh from
already-persisted, operator-registered state every call.
"""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from patchfrog.contract_intelligence.domain import ContractDelta
from patchfrog.cross_repo_intelligence.domain import (
    CROSS_REPO_INTELLIGENCE_VERSION,
    CrossRepoIntelligenceReport,
)
from patchfrog.cross_repo_intelligence.matching import derive_cross_repo_signals
from patchfrog.cross_repo_intelligence.queries import fetch_cross_repo_overlaps


def _changed_contract_surfaces(deltas: tuple[ContractDelta, ...]) -> tuple[tuple[str, str], ...]:
    surfaces: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for delta in deltas:
        key = (delta.file_path, delta.qualified_name)
        if key in seen:
            continue
        seen.add(key)
        surfaces.append(key)
    return tuple(surfaces)


async def build_cross_repo_intelligence_report(
    session: AsyncSession,
    *,
    repository_id: uuid.UUID,
    contract_deltas: tuple[ContractDelta, ...] = (),
) -> CrossRepoIntelligenceReport:
    """Empty report when the current PR has no real contract delta at
    all -- cross-repo scope has no meaning without a changed contract
    surface to check against registered relations."""

    changed_surfaces = _changed_contract_surfaces(contract_deltas)
    if not changed_surfaces:
        return CrossRepoIntelligenceReport(
            version=CROSS_REPO_INTELLIGENCE_VERSION, peers_considered=(), overlaps=(), signals=(),
        )

    overlaps = await fetch_cross_repo_overlaps(
        session, repository_id=repository_id, changed_surfaces=changed_surfaces,
    )
    signals = derive_cross_repo_signals(overlaps)

    peers_considered = []
    seen_peers: set[uuid.UUID] = set()
    for overlap in overlaps:
        if overlap.peer.repository_id in seen_peers:
            continue
        seen_peers.add(overlap.peer.repository_id)
        peers_considered.append(overlap.peer)

    return CrossRepoIntelligenceReport(
        version=CROSS_REPO_INTELLIGENCE_VERSION, peers_considered=tuple(peers_considered),
        overlaps=overlaps, signals=signals,
    )
