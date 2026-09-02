"""Expected/missing companion-change candidates.

Exactly two graph-grounded heuristics -- see the module docstring of
:mod:`patchfrog.change_intelligence.domain` (``CompanionReasonCode``)
for why the taxonomy stops here. Every candidate this module produces
traces back to one real, already-persisted edge (a call, or a
``FILE_TESTS_FILE`` relationship) -- never a naming guess, never
prose-based inference.

**These are candidates, never findings.** Nothing here is ever
published directly to GitHub -- see
:mod:`patchfrog.change_intelligence.service` for how a small, bounded
summary of ``MISSING`` candidates is threaded into the existing
reviewer's own evidence package, to be independently verified (or
rejected) by the real review/critic pipeline like any other evidence.
"""

from __future__ import annotations

from collections.abc import Set as AbstractSet
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from patchfrog.change_intelligence.domain import (
    MAX_FANOUT_PER_SYMBOL,
    CompanionReasonCode,
    CompanionStatus,
    ExpectedCompanionChange,
)
from patchfrog.intelligence.queries import RepositoryQueryService
from patchfrog.review.domain import ReviewCandidate


async def derive_expected_companions(
    session: AsyncSession,
    *,
    change_unit_id: str,
    changed_candidates: tuple[ReviewCandidate, ...],
    all_changed_symbol_ids: AbstractSet[UUID],
    all_changed_file_paths: AbstractSet[str],
    query_service: RepositoryQueryService | None = None,
) -> tuple[ExpectedCompanionChange, ...]:
    queries = query_service or RepositoryQueryService()
    results: list[ExpectedCompanionChange] = []

    results.extend(
        await _caller_staleness(
            session,
            queries,
            change_unit_id=change_unit_id,
            changed_candidates=changed_candidates,
            all_changed_symbol_ids=all_changed_symbol_ids,
        )
    )
    results.extend(
        await _test_staleness(
            session,
            queries,
            change_unit_id=change_unit_id,
            changed_candidates=changed_candidates,
            all_changed_file_paths=all_changed_file_paths,
        )
    )
    return tuple(results)


async def _caller_staleness(
    session: AsyncSession,
    queries: RepositoryQueryService,
    *,
    change_unit_id: str,
    changed_candidates: tuple[ReviewCandidate, ...],
    all_changed_symbol_ids: AbstractSet[UUID],
) -> list[ExpectedCompanionChange]:
    out: list[ExpectedCompanionChange] = []
    seen_pairs: set[tuple[str, str]] = set()

    for candidate in changed_candidates:
        if candidate.symbol_id is None or candidate.qualified_name is None:
            continue
        callers = await queries.get_callers(session, symbol_id=candidate.symbol_id)
        for ref in callers[:MAX_FANOUT_PER_SYMBOL]:
            if ref.caller_symbol_id is None:
                continue  # unresolved caller -- not specific enough to name (spec section 7)
            caller_symbol = await queries.get_symbol_by_id(session, symbol_id=ref.caller_symbol_id)
            if caller_symbol is None:
                continue
            caller_file = await queries.get_file_by_id(session, indexed_file_id=caller_symbol.indexed_file_id)
            if caller_file is None:
                continue
            pair_key = (candidate.qualified_name, caller_symbol.qualified_name)
            if pair_key in seen_pairs:
                continue
            seen_pairs.add(pair_key)
            status = (
                CompanionStatus.OBSERVED
                if ref.caller_symbol_id in all_changed_symbol_ids
                else CompanionStatus.MISSING
            )
            out.append(
                ExpectedCompanionChange(
                    change_unit_id=change_unit_id,
                    source_qualified_name=candidate.qualified_name,
                    source_file_path=candidate.file_path,
                    expected_qualified_name=caller_symbol.qualified_name,
                    expected_file_path=caller_file.relative_path,
                    reason_code=CompanionReasonCode.CALLER_NOT_UPDATED,
                    reason=(
                        f"{caller_symbol.qualified_name!r} calls the changed symbol "
                        f"{candidate.qualified_name!r}"
                    ),
                    evidence=f"call edge: {caller_symbol.qualified_name} -> {candidate.qualified_name}",
                    status=status,
                )
            )
    return out


async def _test_staleness(
    session: AsyncSession,
    queries: RepositoryQueryService,
    *,
    change_unit_id: str,
    changed_candidates: tuple[ReviewCandidate, ...],
    all_changed_file_paths: AbstractSet[str],
) -> list[ExpectedCompanionChange]:
    out: list[ExpectedCompanionChange] = []
    seen_files: set[str] = set()

    for candidate in changed_candidates:
        if candidate.file_path in seen_files:
            continue
        seen_files.add(candidate.file_path)
        if candidate.symbol_id is not None:
            symbol = await queries.get_symbol_by_id(session, symbol_id=candidate.symbol_id)
            if symbol is None:
                continue
            file_id = symbol.indexed_file_id
        else:
            continue

        edges = await queries.likely_tests_for_file(session, indexed_file_id=file_id)
        seen_test_files: set[str] = set()
        for edge in edges:
            test_file = await queries.get_file_by_id(session, indexed_file_id=edge.source_file_id)
            if test_file is None or test_file.relative_path in seen_test_files:
                continue
            seen_test_files.add(test_file.relative_path)
            status = (
                CompanionStatus.OBSERVED
                if test_file.relative_path in all_changed_file_paths
                else CompanionStatus.MISSING
            )
            out.append(
                ExpectedCompanionChange(
                    change_unit_id=change_unit_id,
                    source_qualified_name=candidate.qualified_name or candidate.file_path,
                    source_file_path=candidate.file_path,
                    expected_qualified_name=test_file.relative_path,
                    expected_file_path=test_file.relative_path,
                    reason_code=CompanionReasonCode.TEST_NOT_UPDATED,
                    reason=(
                        f"{test_file.relative_path!r} is a likely test for the changed file "
                        f"{candidate.file_path!r}"
                    ),
                    evidence=edge.reason or "file_tests_file edge",
                    status=status,
                )
            )
    return out
