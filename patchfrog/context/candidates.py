"""Deterministic context candidate generation.

Every candidate here comes directly from a Phase 2 repository-intelligence
query (:class:`~patchfrog.intelligence.queries.RepositoryQueryService`) --
never a guess, never a semantic inference. If a relationship isn't
resolvable (unresolved call, external import, ambiguous test match), it
simply produces no candidate rather than a fabricated one.

Expansion is conservative by design (see the module-level docstring in
``patchfrog/context/config.py`` for ``graph_depth``): depth 1 covers
direct callers/callees/tests/imports; depth 2 additionally considers
callers-of-callers and callees-of-callees, bounded to avoid crawling the
whole graph.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from patchfrog.context.config import ContextConfig
from patchfrog.context.domain import ContextCandidate, ContextItemKind, ContextRelationship
from patchfrog.intelligence.queries import RepositoryQueryService
from patchfrog.persistence.models.code_index import IndexedFileModel, SymbolModel

#: Bound on how many depth-1 caller/callee nodes are expanded to depth 2 --
#: without this, a highly-connected symbol could trigger an expansion
#: proportional to the whole call graph.
_MAX_DEPTH_2_EXPANSION_ROOTS = 5
#: Bound on same-file sibling candidates considered (previous/next symbol
#: only) -- deliberately not "every sibling in the file".
_MAX_SIBLINGS = 2


@dataclass(frozen=True, slots=True)
class _TargetContext:
    symbol: SymbolModel | None
    file: IndexedFileModel


class ContextCandidateGenerator:
    def __init__(self, *, query_service: RepositoryQueryService | None = None) -> None:
        self._queries = query_service or RepositoryQueryService()

    async def generate(
        self,
        session: AsyncSession,
        *,
        repository_index_id: UUID,
        target_file: IndexedFileModel,
        target_symbol: SymbolModel | None,
        target_line: int | None,
        config: ContextConfig,
        changed_lines_by_file: dict[str, frozenset[int]],
    ) -> list[ContextCandidate]:
        candidates: list[ContextCandidate] = []
        target = _TargetContext(symbol=target_symbol, file=target_file)

        candidates.append(self._target_candidate(target, target_line, changed_lines_by_file))

        if target_symbol is not None:
            if config.wants(ContextItemKind.PARENT_SYMBOL) and target_symbol.parent_symbol_id is not None:
                parent = await self._queries.get_symbol_by_id(session, symbol_id=target_symbol.parent_symbol_id)
                if parent is not None:
                    parent_file = await self._file_for_symbol(session, parent)
                    if parent_file is not None:
                        candidates.append(
                            self._symbol_candidate(
                                parent,
                                parent_file,
                                relationship=ContextRelationship.PARENT_SYMBOL,
                                kind=ContextItemKind.PARENT_SYMBOL,
                                distance=1,
                                reason=f"parent of target symbol {target_symbol.qualified_name!r}",
                                changed_lines_by_file=changed_lines_by_file,
                            )
                        )

            if config.wants(ContextItemKind.SIBLING_SYMBOL):
                candidates.extend(
                    await self._sibling_candidates(session, target_symbol, target_file, changed_lines_by_file)
                )

            if config.wants(ContextItemKind.CALLER):
                candidates.extend(
                    await self._caller_candidates(session, target_symbol, config, changed_lines_by_file)
                )
            if config.wants(ContextItemKind.CALLEE):
                candidates.extend(
                    await self._callee_candidates(session, target_symbol, config, changed_lines_by_file)
                )

        if config.wants(ContextItemKind.RELATED_TEST):
            candidates.extend(
                await self._test_candidates(session, target_file, target_symbol, changed_lines_by_file)
            )

        if config.wants(ContextItemKind.IMPORTED_DEPENDENCY) or config.wants(ContextItemKind.INCLUDED_HEADER):
            candidates.extend(
                await self._import_candidates(session, target_file, config, changed_lines_by_file)
            )

        return candidates

    def _target_candidate(
        self,
        target: _TargetContext,
        target_line: int | None,
        changed_lines_by_file: dict[str, frozenset[int]],
    ) -> ContextCandidate:
        if target.symbol is not None:
            return self._symbol_candidate(
                target.symbol,
                target.file,
                relationship=ContextRelationship.TARGET_SYMBOL,
                kind=ContextItemKind.TARGET_SYMBOL,
                distance=0,
                reason="the target symbol itself",
                changed_lines_by_file=changed_lines_by_file,
            )

        line = target_line if target_line is not None else 1
        window_start = max(1, line - 15)
        window_end = line + 15
        changed = changed_lines_by_file.get(target.file.relative_path, frozenset())
        return ContextCandidate(
            kind=ContextItemKind.TARGET_FILE_REGION,
            file_path=target.file.relative_path,
            symbol_id=None,
            symbol_name=None,
            qualified_name=None,
            start_line=window_start,
            end_line=window_end,
            relationship=ContextRelationship.TARGET_SYMBOL,
            distance=0,
            reason="target line has no containing symbol (module-level code)",
            is_on_changed_line=any(line_no in changed for line_no in range(window_start, window_end + 1)),
        )

    def _symbol_candidate(
        self,
        symbol: SymbolModel,
        file: IndexedFileModel,
        *,
        relationship: ContextRelationship,
        kind: ContextItemKind,
        distance: int,
        reason: str,
        changed_lines_by_file: dict[str, frozenset[int]],
    ) -> ContextCandidate:
        changed = changed_lines_by_file.get(file.relative_path, frozenset())
        return ContextCandidate(
            kind=kind,
            file_path=file.relative_path,
            symbol_id=symbol.id,
            symbol_name=symbol.name,
            qualified_name=symbol.qualified_name,
            start_line=symbol.start_line,
            end_line=symbol.end_line,
            relationship=relationship,
            distance=distance,
            reason=reason,
            is_on_changed_line=any(line in changed for line in range(symbol.start_line, symbol.end_line + 1)),
        )

    async def _file_for_symbol(self, session: AsyncSession, symbol: SymbolModel) -> IndexedFileModel | None:
        return await self._queries.get_file_by_id(session, indexed_file_id=symbol.indexed_file_id)

    async def _sibling_candidates(
        self,
        session: AsyncSession,
        target_symbol: SymbolModel,
        target_file: IndexedFileModel,
        changed_lines_by_file: dict[str, frozenset[int]],
    ) -> list[ContextCandidate]:
        siblings = await self._queries.symbols_in_file(session, indexed_file_id=target_file.id)
        same_parent = [
            s
            for s in siblings
            if s.id != target_symbol.id and s.parent_symbol_id == target_symbol.parent_symbol_id
        ]
        before = [s for s in same_parent if s.end_line < target_symbol.start_line]
        after = [s for s in same_parent if s.start_line > target_symbol.end_line]
        chosen = ([before[-1]] if before else []) + ([after[0]] if after else [])
        return [
            self._symbol_candidate(
                sibling,
                target_file,
                relationship=ContextRelationship.SIBLING_SYMBOL,
                kind=ContextItemKind.SIBLING_SYMBOL,
                distance=1,
                reason=f"adjacent symbol in the same file as {target_symbol.qualified_name!r}",
                changed_lines_by_file=changed_lines_by_file,
            )
            for sibling in chosen[:_MAX_SIBLINGS]
        ]

    async def _caller_candidates(
        self,
        session: AsyncSession,
        target_symbol: SymbolModel,
        config: ContextConfig,
        changed_lines_by_file: dict[str, frozenset[int]],
    ) -> list[ContextCandidate]:
        return await self._call_edge_candidates(
            session,
            symbol_ids=[target_symbol.id],
            direction="callers",
            relationship=ContextRelationship.DIRECT_CALLER,
            transitive_relationship=ContextRelationship.TRANSITIVE_CALLER,
            kind=ContextItemKind.CALLER,
            config=config,
            changed_lines_by_file=changed_lines_by_file,
        )

    async def _callee_candidates(
        self,
        session: AsyncSession,
        target_symbol: SymbolModel,
        config: ContextConfig,
        changed_lines_by_file: dict[str, frozenset[int]],
    ) -> list[ContextCandidate]:
        return await self._call_edge_candidates(
            session,
            symbol_ids=[target_symbol.id],
            direction="callees",
            relationship=ContextRelationship.DIRECT_CALLEE,
            transitive_relationship=ContextRelationship.TRANSITIVE_CALLEE,
            kind=ContextItemKind.CALLEE,
            config=config,
            changed_lines_by_file=changed_lines_by_file,
        )

    async def _related_symbol_ids(
        self, session: AsyncSession, *, symbol_id: UUID, direction: str
    ) -> set[UUID]:
        refs = (
            await self._queries.get_callers(session, symbol_id=symbol_id)
            if direction == "callers"
            else await self._queries.get_callees(session, symbol_id=symbol_id)
        )
        return (
            {r.caller_symbol_id for r in refs if r.caller_symbol_id is not None}
            if direction == "callers"
            else {r.resolved_symbol_id for r in refs if r.resolved_symbol_id is not None}
        )

    async def _resolve_symbols_and_files(
        self, session: AsyncSession, *, symbol_ids: set[UUID]
    ) -> list[tuple[SymbolModel, IndexedFileModel]]:
        """Batched: one query for every symbol id, one query for every
        distinct file id those symbols live in -- never a per-id
        round-trip, which a highly-connected symbol (many callers/callees)
        would otherwise turn into hundreds of individual queries."""

        if not symbol_ids:
            return []
        symbols_by_id = await self._queries.get_symbols_by_ids(session, symbol_ids=sorted(symbol_ids, key=str))
        file_ids = {s.indexed_file_id for s in symbols_by_id.values()}
        files_by_id = await self._queries.get_files_by_ids(session, indexed_file_ids=sorted(file_ids, key=str))
        resolved = []
        for symbol_id in sorted(symbol_ids, key=str):
            symbol = symbols_by_id.get(symbol_id)
            if symbol is None:
                continue
            file = files_by_id.get(symbol.indexed_file_id)
            if file is None:
                continue
            resolved.append((symbol, file))
        return resolved

    async def _call_edge_candidates(
        self,
        session: AsyncSession,
        *,
        symbol_ids: list[UUID],
        direction: str,
        relationship: ContextRelationship,
        transitive_relationship: ContextRelationship,
        kind: ContextItemKind,
        config: ContextConfig,
        changed_lines_by_file: dict[str, frozenset[int]],
    ) -> list[ContextCandidate]:
        depth_1_symbol_ids: set[UUID] = set()
        for symbol_id in symbol_ids:
            depth_1_symbol_ids |= await self._related_symbol_ids(session, symbol_id=symbol_id, direction=direction)

        candidates: list[ContextCandidate] = []
        for symbol, file in await self._resolve_symbols_and_files(session, symbol_ids=depth_1_symbol_ids):
            candidates.append(
                self._symbol_candidate(
                    symbol,
                    file,
                    relationship=relationship,
                    kind=kind,
                    distance=1,
                    reason=f"direct {direction[:-1]} of the target symbol",
                    changed_lines_by_file=changed_lines_by_file,
                )
            )

        if config.graph_depth >= 2 and depth_1_symbol_ids:
            roots = sorted(depth_1_symbol_ids, key=str)[:_MAX_DEPTH_2_EXPANSION_ROOTS]
            depth_2_symbol_ids: set[UUID] = set()
            for root_id in roots:
                depth_2_symbol_ids |= await self._related_symbol_ids(session, symbol_id=root_id, direction=direction)
            depth_2_symbol_ids -= depth_1_symbol_ids

            for symbol, file in await self._resolve_symbols_and_files(session, symbol_ids=depth_2_symbol_ids):
                candidates.append(
                    self._symbol_candidate(
                        symbol,
                        file,
                        relationship=transitive_relationship,
                        kind=kind,
                        distance=2,
                        reason=f"transitive {direction[:-1]} (depth 2) of the target symbol",
                        changed_lines_by_file=changed_lines_by_file,
                    )
                )

        return candidates

    async def _test_candidates(
        self,
        session: AsyncSession,
        target_file: IndexedFileModel,
        target_symbol: SymbolModel | None,
        changed_lines_by_file: dict[str, frozenset[int]],
    ) -> list[ContextCandidate]:
        edges = await self._queries.likely_tests_for_file(session, indexed_file_id=target_file.id)
        candidates: list[ContextCandidate] = []
        seen_files: set[UUID] = set()
        for edge in edges:
            if edge.source_file_id in seen_files:
                continue
            seen_files.add(edge.source_file_id)
            test_file = await self._queries.get_file_by_id(session, indexed_file_id=edge.source_file_id)
            if test_file is None:
                continue
            test_symbols = await self._queries.symbols_in_file(session, indexed_file_id=test_file.id)
            chosen_symbol = _best_matching_test_symbol(test_symbols, target_symbol)
            reason = edge.reason or "likely test for this file"
            if chosen_symbol is not None:
                candidates.append(
                    self._symbol_candidate(
                        chosen_symbol,
                        test_file,
                        relationship=ContextRelationship.TESTS_TARGET_FILE,
                        kind=ContextItemKind.RELATED_TEST,
                        distance=1,
                        reason=reason,
                        changed_lines_by_file=changed_lines_by_file,
                    )
                )
            else:
                candidates.append(
                    ContextCandidate(
                        kind=ContextItemKind.RELATED_TEST,
                        file_path=test_file.relative_path,
                        symbol_id=None,
                        symbol_name=None,
                        qualified_name=None,
                        start_line=1,
                        end_line=30,
                        relationship=ContextRelationship.TESTS_TARGET_FILE,
                        distance=1,
                        reason=reason,
                    )
                )
        return candidates

    async def _import_candidates(
        self,
        session: AsyncSession,
        target_file: IndexedFileModel,
        config: ContextConfig,
        changed_lines_by_file: dict[str, frozenset[int]],
    ) -> list[ContextCandidate]:
        imports = await self._queries.imports_from_file(session, indexed_file_id=target_file.id)
        candidates: list[ContextCandidate] = []
        seen_files: set[UUID] = set()
        for imp in imports:
            if imp.resolved_file_id is None or imp.resolved_file_id in seen_files:
                continue
            seen_files.add(imp.resolved_file_id)
            dep_file = await self._queries.get_file_by_id(session, indexed_file_id=imp.resolved_file_id)
            if dep_file is None:
                continue
            is_include = imp.raw_text.lstrip().startswith("#include")
            kind = ContextItemKind.INCLUDED_HEADER if is_include else ContextItemKind.IMPORTED_DEPENDENCY
            relationship = (
                ContextRelationship.INCLUDE_DEPENDENCY if is_include else ContextRelationship.IMPORT_DEPENDENCY
            )
            if not config.wants(kind):
                continue
            dep_symbols = await self._queries.symbols_in_file(session, indexed_file_id=dep_file.id)
            if dep_symbols:
                first_symbol = min(dep_symbols, key=lambda s: s.start_line)
                candidates.append(
                    self._symbol_candidate(
                        first_symbol,
                        dep_file,
                        relationship=relationship,
                        kind=kind,
                        distance=1,
                        reason=f"{'included' if is_include else 'imported'} by {target_file.relative_path!r}",
                        changed_lines_by_file=changed_lines_by_file,
                    )
                )
            else:
                candidates.append(
                    ContextCandidate(
                        kind=kind,
                        file_path=dep_file.relative_path,
                        symbol_id=None,
                        symbol_name=None,
                        qualified_name=None,
                        start_line=1,
                        end_line=30,
                        relationship=relationship,
                        distance=1,
                        reason=f"{'included' if is_include else 'imported'} by {target_file.relative_path!r}",
                    )
                )
        return candidates


def _best_matching_test_symbol(test_symbols: list[SymbolModel], target_symbol: SymbolModel | None) -> SymbolModel | None:
    if not test_symbols:
        return None
    if target_symbol is not None:
        needle = target_symbol.name.lower()
        matches = [s for s in test_symbols if needle in s.name.lower()]
        if matches:
            return min(matches, key=lambda s: s.start_line)
    return min(test_symbols, key=lambda s: s.start_line)
