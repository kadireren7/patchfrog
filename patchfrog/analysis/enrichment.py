"""Repository-intelligence enrichment for findings.

Attaches containing-symbol identity (Phase 2's ``file + line -> symbol``
query) and changed-line/changed-symbol classification to each finding, and
computes its fingerprint once the symbol anchor is known.

Batches lookups per file rather than per finding — many findings in one
analysis run share a handful of files, and Phase 2's own audit found this
exact per-item-query pattern to be a real N+1 (see
``ParsedFileCacheRepository.get_many``); this enricher fetches each file's
row and full symbol list once and reuses them for every finding in that
file.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from patchfrog.analysis.changed_lines import classify
from patchfrog.analysis.domain import RawFinding
from patchfrog.analysis.fingerprint import compute_fingerprint
from patchfrog.domain.code import SourceSpan
from patchfrog.intelligence.queries import RepositoryQueryService
from patchfrog.persistence.models.code_index import IndexedFileModel, SymbolModel


@dataclass(frozen=True, slots=True)
class EnrichedFinding:
    finding: RawFinding
    fingerprint: str
    symbol_id: uuid.UUID | None
    symbol_name: str | None
    symbol_qualified_name: str | None
    is_on_changed_line: bool
    is_in_changed_symbol: bool


class FindingEnricher:
    def __init__(self, *, query_service: RepositoryQueryService | None = None) -> None:
        self._queries = query_service or RepositoryQueryService()

    async def enrich_many(
        self,
        session: AsyncSession,
        *,
        findings: list[RawFinding],
        repository_index_id: uuid.UUID,
        changed_lines_by_file: dict[str, frozenset[int]],
    ) -> list[EnrichedFinding]:
        file_cache: dict[str, IndexedFileModel | None] = {}
        symbols_cache: dict[uuid.UUID, list[SymbolModel]] = {}
        enriched: list[EnrichedFinding] = []

        for finding in findings:
            if finding.file_path not in file_cache:
                file_cache[finding.file_path] = await self._queries.get_file(
                    session, repository_index_id=repository_index_id, relative_path=finding.file_path
                )
            file_row = file_cache[finding.file_path]

            symbol: SymbolModel | None = None
            if file_row is not None:
                if file_row.id not in symbols_cache:
                    symbols_cache[file_row.id] = await self._queries.symbols_in_file(
                        session, indexed_file_id=file_row.id
                    )
                symbol = _innermost_containing_symbol(symbols_cache[file_row.id], finding.span.start_line)

            symbol_qualified_name = symbol.qualified_name if symbol is not None else None
            fingerprint = compute_fingerprint(finding, symbol_qualified_name=symbol_qualified_name)

            containing_symbol_span = (
                SourceSpan(symbol.start_line, symbol.end_line, symbol.start_column, symbol.end_column)
                if symbol is not None
                else None
            )
            classification = classify(
                file_path=finding.file_path,
                span=finding.span,
                changed_lines_by_file=changed_lines_by_file,
                containing_symbol_span=containing_symbol_span,
            )

            enriched.append(
                EnrichedFinding(
                    finding=finding,
                    fingerprint=fingerprint,
                    symbol_id=symbol.id if symbol is not None else None,
                    symbol_name=symbol.name if symbol is not None else None,
                    symbol_qualified_name=symbol_qualified_name,
                    is_on_changed_line=classification.is_on_changed_line,
                    is_in_changed_symbol=classification.is_in_changed_symbol,
                )
            )

        return enriched


def _innermost_containing_symbol(symbols: list[SymbolModel], line: int) -> SymbolModel | None:
    candidates = [s for s in symbols if s.start_line <= line <= s.end_line]
    if not candidates:
        return None
    return min(candidates, key=lambda s: s.end_line - s.start_line)
