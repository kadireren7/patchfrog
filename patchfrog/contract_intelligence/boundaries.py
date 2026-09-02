"""Contract-boundary eligibility (spec section 4): a symbol only becomes
a meaningful contract when there is real evidence something consumes
it. Internal leaf helpers with zero resolved callers never produce
Contract Intelligence -- this is the gate that keeps this package from
labeling "every function" a contract.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from patchfrog.domain.code import Language, SymbolKind
from patchfrog.intelligence.queries import RepositoryQueryService
from patchfrog.persistence.models.code_index import IndexedFileModel, SymbolModel
from patchfrog.review.domain import ReviewCandidate

#: Only these symbol kinds can carry a function-style signature.
_FUNCTION_LIKE = (SymbolKind.FUNCTION, SymbolKind.METHOD)

#: Only Python signatures are parsed structurally this milestone -- see
#: ``validation/contract_intelligence/latest-summary.md`` section 1.
_SUPPORTED_LANGUAGES = (Language.PYTHON,)


@dataclass(frozen=True, slots=True)
class ContractEligibleCandidate:
    """One changed candidate confirmed (against the real index) to be a
    Python function/method with at least one real, resolved caller."""

    candidate: ReviewCandidate
    symbol: SymbolModel
    file: IndexedFileModel
    caller_count: int


async def find_contract_eligible_candidates(
    session: AsyncSession,
    *,
    candidates: tuple[ReviewCandidate, ...],
    query_service: RepositoryQueryService | None = None,
) -> tuple[ContractEligibleCandidate, ...]:
    queries = query_service or RepositoryQueryService()
    out: list[ContractEligibleCandidate] = []

    for candidate in candidates:
        if candidate.symbol_id is None or candidate.qualified_name is None:
            continue
        symbol = await queries.get_symbol_by_id(session, symbol_id=candidate.symbol_id)
        if symbol is None or symbol.kind not in _FUNCTION_LIKE or symbol.language not in _SUPPORTED_LANGUAGES:
            continue
        if symbol.signature is None:
            continue
        file = await queries.get_file_by_id(session, indexed_file_id=symbol.indexed_file_id)
        if file is None:
            continue
        callers = await queries.get_callers(session, symbol_id=candidate.symbol_id)
        resolved_caller_ids = {r.caller_symbol_id for r in callers if r.caller_symbol_id is not None}
        if not resolved_caller_ids:
            continue  # no real consumer evidence -- not a contract boundary
        out.append(
            ContractEligibleCandidate(
                candidate=candidate, symbol=symbol, file=file, caller_count=len(resolved_caller_ids)
            )
        )
    return tuple(out)


def needed_base_file_paths(eligible: tuple[ContractEligibleCandidate, ...]) -> frozenset[str]:
    return frozenset(e.candidate.file_path for e in eligible)
