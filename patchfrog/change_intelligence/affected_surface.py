"""Bounded affected-surface derivation for one
:class:`~patchfrog.change_intelligence.domain.ChangeUnit`.

Reuses the exact same query primitives
(:class:`~patchfrog.intelligence.queries.RepositoryQueryService`) and
the same depth-1-then-2 bounding discipline as
:mod:`patchfrog.context.candidates`'s ``_call_edge_candidates`` --
deliberately not that class itself (it is single-target/token-budget
shaped; this is multi-root/unit shaped) -- so "affected surface" and
"context the reviewer sees" can never silently diverge in what counts
as a real dependency.

Never traverses the whole repository: every symbol's fan-out is capped
at :data:`~patchfrog.change_intelligence.domain.MAX_FANOUT_PER_SYMBOL`,
depth is capped at :data:`~patchfrog.change_intelligence.domain.MAX_GRAPH_DEPTH`,
and the total affected surface per unit is capped at
:data:`~patchfrog.change_intelligence.domain.MAX_AFFECTED_SURFACE_PER_UNIT`
(deterministic truncation -- sorted by ``(distance, file_path, qualified_name)``
before capping, so the same input always keeps the same subset, and
tests/direct-dependents are always ranked ahead of merely indirect
ones since they're structurally closer/more actionable evidence).
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from patchfrog.change_intelligence.domain import (
    MAX_AFFECTED_SURFACE_PER_UNIT,
    MAX_FANOUT_PER_SYMBOL,
    MAX_GRAPH_DEPTH,
    AffectedRelation,
    AffectedSymbolRef,
)
from patchfrog.intelligence.queries import RepositoryQueryService
from patchfrog.persistence.models.code_index import IndexedFileModel, SymbolModel
from patchfrog.review.domain import ReviewCandidate

_RELATION_RANK: dict[AffectedRelation, int] = {
    AffectedRelation.TEST: 0,
    AffectedRelation.DIRECTLY_DEPENDENT: 1,
    AffectedRelation.INDIRECTLY_AFFECTED: 2,
    AffectedRelation.DIRECTLY_CHANGED: 3,
}


async def derive_affected_surface(
    session: AsyncSession,
    *,
    changed_candidates: tuple[ReviewCandidate, ...],
    query_service: RepositoryQueryService | None = None,
) -> tuple[AffectedSymbolRef, ...]:
    queries = query_service or RepositoryQueryService()

    changed_symbol_ids = {c.symbol_id for c in changed_candidates if c.symbol_id is not None}
    if not changed_symbol_ids:
        return ()

    found: dict[UUID, AffectedSymbolRef] = {}

    depth_1_ids: set[UUID] = set()
    for symbol_id in sorted(changed_symbol_ids, key=str):
        callers = await queries.get_callers(session, symbol_id=symbol_id)
        for ref in callers[:MAX_FANOUT_PER_SYMBOL]:
            if ref.caller_symbol_id is not None and ref.caller_symbol_id not in changed_symbol_ids:
                depth_1_ids.add(ref.caller_symbol_id)
        callees = await queries.get_callees(session, symbol_id=symbol_id)
        for ref in callees[:MAX_FANOUT_PER_SYMBOL]:
            if ref.resolved_symbol_id is not None and ref.resolved_symbol_id not in changed_symbol_ids:
                depth_1_ids.add(ref.resolved_symbol_id)

    for symbol, file in await _resolve(queries, session, depth_1_ids):
        found[symbol.id] = AffectedSymbolRef(
            file_path=file.relative_path,
            qualified_name=symbol.qualified_name,
            symbol_name=symbol.name,
            relation=AffectedRelation.DIRECTLY_DEPENDENT,
            distance=1,
            reason=f"directly calls or is called by a changed symbol ({symbol.qualified_name!r})",
        )

    if MAX_GRAPH_DEPTH >= 2 and depth_1_ids:
        depth_2_ids: set[UUID] = set()
        for symbol_id in sorted(depth_1_ids, key=str):
            callers = await queries.get_callers(session, symbol_id=symbol_id)
            for ref in callers[:MAX_FANOUT_PER_SYMBOL]:
                if (
                    ref.caller_symbol_id is not None
                    and ref.caller_symbol_id not in changed_symbol_ids
                    and ref.caller_symbol_id not in depth_1_ids
                ):
                    depth_2_ids.add(ref.caller_symbol_id)
            callees = await queries.get_callees(session, symbol_id=symbol_id)
            for ref in callees[:MAX_FANOUT_PER_SYMBOL]:
                if (
                    ref.resolved_symbol_id is not None
                    and ref.resolved_symbol_id not in changed_symbol_ids
                    and ref.resolved_symbol_id not in depth_1_ids
                ):
                    depth_2_ids.add(ref.resolved_symbol_id)

        for symbol, file in await _resolve(queries, session, depth_2_ids):
            if symbol.id in found:
                continue
            found[symbol.id] = AffectedSymbolRef(
                file_path=file.relative_path,
                qualified_name=symbol.qualified_name,
                symbol_name=symbol.name,
                relation=AffectedRelation.INDIRECTLY_AFFECTED,
                distance=2,
                reason=f"transitively connected (2 hops) to a changed symbol via {symbol.qualified_name!r}",
            )

    seen_test_files: set[UUID] = set()
    changed_symbols_by_id = await queries.get_symbols_by_ids(session, symbol_ids=sorted(changed_symbol_ids, key=str))
    changed_file_ids = {s.indexed_file_id for s in changed_symbols_by_id.values()}
    for file_id in sorted(changed_file_ids, key=str):
        edges = await queries.likely_tests_for_file(session, indexed_file_id=file_id)
        for edge in edges:
            if edge.source_file_id in seen_test_files:
                continue
            seen_test_files.add(edge.source_file_id)
            test_file = await queries.get_file_by_id(session, indexed_file_id=edge.source_file_id)
            if test_file is None:
                continue
            key_id = edge.source_file_id
            found[key_id] = AffectedSymbolRef(
                file_path=test_file.relative_path,
                qualified_name=None,
                symbol_name=None,
                relation=AffectedRelation.TEST,
                distance=1,
                reason=edge.reason or "likely test for a changed file",
            )

    ordered = sorted(
        found.values(),
        key=lambda ref: (_RELATION_RANK[ref.relation], ref.file_path, ref.qualified_name or ""),
    )
    return tuple(ordered[:MAX_AFFECTED_SURFACE_PER_UNIT])


async def _resolve(
    queries: RepositoryQueryService, session: AsyncSession, symbol_ids: set[UUID]
) -> list[tuple[SymbolModel, IndexedFileModel]]:
    if not symbol_ids:
        return []
    symbols_by_id = await queries.get_symbols_by_ids(session, symbol_ids=sorted(symbol_ids, key=str))
    file_ids = {s.indexed_file_id for s in symbols_by_id.values()}
    files_by_id = await queries.get_files_by_ids(session, indexed_file_ids=sorted(file_ids, key=str))
    resolved: list[tuple[SymbolModel, IndexedFileModel]] = []
    for symbol_id in sorted(symbol_ids, key=str):
        symbol = symbols_by_id.get(symbol_id)
        if symbol is None:
            continue
        file = files_by_id.get(symbol.indexed_file_id)
        if file is None:
            continue
        resolved.append((symbol, file))
    return resolved
