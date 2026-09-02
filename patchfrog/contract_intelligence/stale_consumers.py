"""Stale-consumer candidate derivation (spec section 8).

A :class:`~patchfrog.change_intelligence.domain.ExpectedCompanionChange`
(reason code ``CONTRACT_CONSUMER_NOT_UPDATED``) is produced only when
**all** of the following hold, mirroring
:mod:`patchfrog.change_intelligence.companions`'s own discipline exactly:

1. a real :class:`~patchfrog.contract_intelligence.domain.ContractDelta`
   exists (base and head signatures were both parsed, and differ)
2. a real, currently-resolved caller relation exists
   (:meth:`patchfrog.intelligence.queries.RepositoryQueryService.get_callers`
   against the exact reviewed HEAD index -- always current, never stale)
3. that caller was not itself changed in this diff
4. the delta's characteristics are in
   :data:`patchfrog.contract_intelligence.domain.BREAKING_CHARACTERISTICS`
   (requirement 4: "the delta characteristic plausibly requires
   consumer adaptation")
5. the caller is named specifically (a real symbol, never a vague
   "something might be affected")

**Never auto-published** -- these are candidates only, exactly like
every other :class:`ExpectedCompanionChange` (spec section 9).
"""

from __future__ import annotations

from collections.abc import Set as AbstractSet
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from patchfrog.change_intelligence.domain import (
    CompanionReasonCode,
    CompanionStatus,
    ExpectedCompanionChange,
)
from patchfrog.contract_intelligence.domain import ContractDelta
from patchfrog.intelligence.queries import RepositoryQueryService


async def derive_stale_consumers(
    session: AsyncSession,
    *,
    deltas: tuple[ContractDelta, ...],
    all_changed_symbol_ids: AbstractSet[UUID],
    contract_symbol_ids: dict[str, UUID],
    query_service: RepositoryQueryService | None = None,
) -> tuple[ExpectedCompanionChange, ...]:
    """``contract_symbol_ids`` maps ``ContractDelta.contract_id ->`` the
    HEAD ``SymbolModel.id`` it describes (the caller lookup needs the
    real symbol id, which the pure :class:`ContractDelta` dataclass
    deliberately doesn't carry -- see its own docstring)."""

    queries = query_service or RepositoryQueryService()
    out: list[ExpectedCompanionChange] = []
    seen_pairs: set[tuple[str, str]] = set()

    for delta in deltas:
        if not delta.is_potentially_breaking:
            continue
        symbol_id = contract_symbol_ids.get(delta.contract_id)
        if symbol_id is None:
            continue

        callers = await queries.get_callers(session, symbol_id=symbol_id)
        for ref in callers:
            if ref.caller_symbol_id is None:
                continue  # unresolved caller -- not specific enough to name
            caller_symbol = await queries.get_symbol_by_id(session, symbol_id=ref.caller_symbol_id)
            if caller_symbol is None:
                continue
            caller_file = await queries.get_file_by_id(session, indexed_file_id=caller_symbol.indexed_file_id)
            if caller_file is None:
                continue
            pair_key = (delta.qualified_name, caller_symbol.qualified_name)
            if pair_key in seen_pairs:
                continue
            seen_pairs.add(pair_key)

            status = (
                CompanionStatus.OBSERVED
                if ref.caller_symbol_id in all_changed_symbol_ids
                else CompanionStatus.MISSING
            )
            characteristic_text = ", ".join(c.value for c in delta.characteristics)
            out.append(
                ExpectedCompanionChange(
                    change_unit_id=delta.change_unit_id or "",
                    source_qualified_name=delta.qualified_name,
                    source_file_path=delta.file_path,
                    expected_qualified_name=caller_symbol.qualified_name,
                    expected_file_path=caller_file.relative_path,
                    reason_code=CompanionReasonCode.CONTRACT_CONSUMER_NOT_UPDATED,
                    reason=(
                        f"{caller_symbol.qualified_name!r} calls {delta.qualified_name!r}, whose contract "
                        f"changed ({characteristic_text}) -- it may still assume the old shape"
                    ),
                    evidence=f"call edge: {caller_symbol.qualified_name} -> {delta.qualified_name}; {delta.evidence}",
                    status=status,
                )
            )
    return tuple(out)
