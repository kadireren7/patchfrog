"""Deterministic multi-root connected-component grouping of a review
run's changed :class:`~patchfrog.review.domain.ReviewCandidate`\\ s into
:class:`~patchfrog.change_intelligence.domain.ChangeUnit`\\ s.

Two changed candidates are grouped into the same unit only when *real*
graph evidence connects them -- never because they share a directory,
never because they're simply "in the same PR". Exactly three connection
kinds, each a real, already-persisted edge:

1. **Calls** -- one changed symbol calls (or is called by) another
   changed symbol (:meth:`~patchfrog.intelligence.queries.RepositoryQueryService.get_callers`/
   ``get_callees``).
2. **Containment** -- one changed symbol is the immediate
   ``parent_symbol_id`` of another (or they share the same immediate
   parent -- sibling methods of the same changed class/struct).
3. **Direct file import** -- one changed candidate's file directly
   imports/includes another changed candidate's file (a real
   ``FILE_IMPORTS_FILE``/``FILE_INCLUDES_FILE`` edge, never "both files
   happen to live in the same directory").

A module-region candidate (no containing symbol -- ``symbol_id is
None``, e.g. a top-level statement) never merges into any unit; it
always stands alone as a bare, single-candidate unit. This is
deliberately conservative: PatchFrog has no reliable graph edge for
"this top-level statement is related to that one," and grouping it
into a symbol's unit anyway would be a guess, not evidence.

Bounded: every symbol's caller/callee fan-out is capped at
:data:`~patchfrog.change_intelligence.domain.MAX_FANOUT_PER_SYMBOL`
candidates considered for grouping is exactly the (already Quality +
Cost Guard-capped) candidate list passed in -- never a separate,
larger traversal.
"""

from __future__ import annotations

from itertools import pairwise
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from patchfrog.change_intelligence.domain import MAX_FANOUT_PER_SYMBOL
from patchfrog.intelligence.queries import RepositoryQueryService
from patchfrog.review.domain import ReviewCandidate


class _UnionFind:
    def __init__(self, n: int) -> None:
        self._parent = list(range(n))

    def find(self, x: int) -> int:
        while self._parent[x] != x:
            self._parent[x] = self._parent[self._parent[x]]
            x = self._parent[x]
        return x

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            # Deterministic tie-break: lower index always becomes root,
            # so component assignment never depends on traversal/dict
            # iteration order.
            if ra < rb:
                self._parent[rb] = ra
            else:
                self._parent[ra] = rb


async def group_into_change_units(
    session: AsyncSession,
    *,
    candidates: list[ReviewCandidate],
    query_service: RepositoryQueryService | None = None,
) -> list[tuple[ReviewCandidate, ...]]:
    """Returns raw grouped candidate tuples, sorted deterministically --
    each tuple is exactly the ``changed_candidates`` of one eventual
    :class:`~patchfrog.change_intelligence.domain.ChangeUnit`. Turning a
    group into a full ``ChangeUnit`` (id, title, kind, affected surface)
    is :mod:`patchfrog.change_intelligence.service`'s job -- this
    function is pure topology, nothing else.

    Deterministic: the same ``candidates`` list (same order) against the
    same index always produces the same groups, in the same order."""

    queries = query_service or RepositoryQueryService()
    if not candidates:
        return []

    symbol_candidate_indices: dict[UUID, list[int]] = {}
    for i, c in enumerate(candidates):
        if c.symbol_id is not None:
            symbol_candidate_indices.setdefault(c.symbol_id, []).append(i)

    changed_symbol_ids = set(symbol_candidate_indices.keys())
    symbols_by_id = await queries.get_symbols_by_ids(session, symbol_ids=sorted(changed_symbol_ids, key=str))

    uf = _UnionFind(len(candidates))

    def _union_symbol_pair(a: UUID, b: UUID) -> None:
        for i in symbol_candidate_indices.get(a, []):
            for j in symbol_candidate_indices.get(b, []):
                uf.union(i, j)

    # 1. Calls -- both directions, since "A calls B" and "B calls A"
    # are equally valid evidence that a change to one plausibly relates
    # to a change to the other.
    for symbol_id in sorted(changed_symbol_ids, key=str):
        callers = await queries.get_callers(session, symbol_id=symbol_id)
        for ref in callers[:MAX_FANOUT_PER_SYMBOL]:
            if ref.caller_symbol_id is not None and ref.caller_symbol_id in changed_symbol_ids:
                _union_symbol_pair(symbol_id, ref.caller_symbol_id)
        callees = await queries.get_callees(session, symbol_id=symbol_id)
        for ref in callees[:MAX_FANOUT_PER_SYMBOL]:
            if ref.resolved_symbol_id is not None and ref.resolved_symbol_id in changed_symbol_ids:
                _union_symbol_pair(symbol_id, ref.resolved_symbol_id)

    # 2. Containment -- parent/child and siblings, among changed symbols only.
    by_parent: dict[UUID, list[UUID]] = {}
    for symbol_id in changed_symbol_ids:
        symbol = symbols_by_id.get(symbol_id)
        if symbol is None:
            continue
        if symbol.parent_symbol_id is not None:
            if symbol.parent_symbol_id in changed_symbol_ids:
                _union_symbol_pair(symbol.parent_symbol_id, symbol_id)
            by_parent.setdefault(symbol.parent_symbol_id, []).append(symbol_id)
    for siblings in by_parent.values():
        for a, b in pairwise(siblings):
            _union_symbol_pair(a, b)

    # 3. Direct file import/include between changed candidates' files.
    file_ids_by_candidate_index: dict[int, UUID] = {}
    for i, c in enumerate(candidates):
        if c.symbol_id is not None:
            symbol = symbols_by_id.get(c.symbol_id)
            if symbol is not None:
                file_ids_by_candidate_index[i] = symbol.indexed_file_id
    distinct_file_ids = sorted(set(file_ids_by_candidate_index.values()), key=str)
    indices_by_file_id: dict[UUID, list[int]] = {}
    for i, fid in file_ids_by_candidate_index.items():
        indices_by_file_id.setdefault(fid, []).append(i)
    for file_id in distinct_file_ids:
        imports = await queries.imports_from_file(session, indexed_file_id=file_id)
        for imp in imports:
            if imp.resolved_file_id is not None and imp.resolved_file_id in indices_by_file_id:
                for i in indices_by_file_id[file_id]:
                    for j in indices_by_file_id[imp.resolved_file_id]:
                        uf.union(i, j)

    # Module-region candidates (no symbol) never merge with anything --
    # already guaranteed: they're never a member of symbol_candidate_indices,
    # file_ids_by_candidate_index, so no union ever touches their index.

    groups: dict[int, list[int]] = {}
    for i in range(len(candidates)):
        root = uf.find(i)
        groups.setdefault(root, []).append(i)

    grouped: list[tuple[ReviewCandidate, ...]] = [
        tuple(sorted((candidates[i] for i in member_indices), key=_candidate_sort_key))
        for member_indices in groups.values()
    ]
    grouped.sort(key=lambda members: _candidate_sort_key(members[0]))
    return grouped


def _candidate_sort_key(c: ReviewCandidate) -> tuple[str, int, str]:
    return (c.file_path, c.start_line, c.qualified_name or c.symbol_name or "")
