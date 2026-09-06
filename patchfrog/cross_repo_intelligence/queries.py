"""Bounded, indexed DB reads this milestone needs. **No cross-repo
crawler, no peer-repository indexing, zero new GitHub API calls.** See
``validation/cross_repo_intelligence/latest-summary.md`` sections
8-11, 17 for the full audit.

:func:`fetch_cross_repo_overlaps` is **one bounded SQL query**: the
current repository's own registered contract keys
(:class:`~patchfrog.persistence.models.cross_repo.RepositoryContractKeyModel`)
matching the current PR's own changed `(file_path, qualified_name)`
surfaces are joined to active
:class:`~patchfrog.persistence.models.cross_repo.RepositoryRelationModel`
rows naming the current repository as producer, joined to the peer
repository -- authorized only when it shares the current repository's
own installation and is still selected. `ORDER BY`/`LIMIT` are applied
in the same query, before any row reaches Python; never a query over
every relation a repository has ever had, and never a peer-by-peer
Python loop (mirrors the exact correction Cross-PR Intelligence's own
`fetch_cross_pr_peers` needed -- see
``validation/cross_pr_intelligence/latest-summary.md`` section 19).

Unlike Cross-PR Intelligence, there is no second query for "peer
structural surfaces": v1's only signal kind needs no live peer
-repository state at all -- the relation registration itself is the
proof of dependency (see latest-summary.md section 8).
"""

from __future__ import annotations

import uuid

from sqlalchemy import select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from patchfrog.cross_repo_intelligence.domain import (
    MAX_CROSS_REPO_PEERS,
    CrossRepoOverlap,
    CrossRepoPeer,
    CrossRepoSignalKind,
    RepositoryRelationKind,
)
from patchfrog.persistence.models.cross_repo import (
    RepositoryContractKeyModel,
    RepositoryRelationModel,
)
from patchfrog.persistence.models.repository import RepositoryModel


async def fetch_cross_repo_overlaps(
    session: AsyncSession,
    *,
    repository_id: uuid.UUID,
    changed_surfaces: tuple[tuple[str, str], ...],
    limit: int = MAX_CROSS_REPO_PEERS,
) -> tuple[CrossRepoOverlap, ...]:
    """Returns one :class:`~patchfrog.cross_repo_intelligence.domain.CrossRepoOverlap`
    per ``(peer, matched surface)`` pair, bounded to ``limit`` rows.
    Empty when ``changed_surfaces`` is empty, when no contract key is
    registered for any of them, when no active relation names a peer
    consumer, or when no peer is authorized under the current
    repository's own installation -- never a crash, never a guess."""

    if not changed_surfaces:
        return ()

    current_repo = aliased(RepositoryModel)
    peer_repo = aliased(RepositoryModel)

    stmt = (
        select(
            peer_repo.id.label("peer_repository_id"),
            peer_repo.full_name.label("peer_full_name"),
            RepositoryRelationModel.relation_kind.label("relation_kind"),
            RepositoryRelationModel.external_contract_key.label("contract_key"),
            RepositoryContractKeyModel.file_path.label("file_path"),
            RepositoryContractKeyModel.qualified_name.label("qualified_name"),
        )
        .select_from(RepositoryContractKeyModel)
        .join(current_repo, current_repo.id == RepositoryContractKeyModel.repository_id)
        .join(
            RepositoryRelationModel,
            (RepositoryRelationModel.source_repository_id == RepositoryContractKeyModel.repository_id)
            & (RepositoryRelationModel.external_contract_key == RepositoryContractKeyModel.stable_key),
        )
        .join(peer_repo, peer_repo.id == RepositoryRelationModel.target_repository_id)
        .where(
            RepositoryContractKeyModel.repository_id == repository_id,
            tuple_(RepositoryContractKeyModel.file_path, RepositoryContractKeyModel.qualified_name).in_(
                changed_surfaces
            ),
            RepositoryRelationModel.relation_kind == RepositoryRelationKind.EXPLICIT_SHARED_CONTRACT,
            RepositoryRelationModel.active.is_(True),
            peer_repo.installation_id == current_repo.installation_id,
            peer_repo.is_selected.is_(True),
        )
        .order_by(RepositoryRelationModel.created_at.desc())
        .limit(limit)
    )

    result = await session.execute(stmt)
    overlaps: list[CrossRepoOverlap] = []
    for row in result.all():
        peer = CrossRepoPeer(
            repository_id=row.peer_repository_id, full_name=row.peer_full_name,
            relation_kind=row.relation_kind, contract_key=row.contract_key,
        )
        overlaps.append(
            CrossRepoOverlap(
                peer=peer, signal_kind=CrossRepoSignalKind.CROSS_REPO_CONTRACT_CHANGE,
                file_path=row.file_path, qualified_name=row.qualified_name,
            )
        )
    return tuple(overlaps)
