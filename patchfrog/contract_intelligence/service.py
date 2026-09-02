"""Top-level Contract & Blast Radius Intelligence orchestrator.

:func:`build_contract_intelligence_report` is the one entry point
everything else in this package composes into. Called at most once per
review run (after Change Intelligence, whose already-built
:class:`~patchfrog.change_intelligence.domain.ChangeUnit`\\ s it reuses
for ``change_unit_id`` attribution) -- see
:mod:`patchfrog.review.service`'s integration point. Zero LLM calls;
the only I/O is a bounded, read-only base-commit file fetch (see
:mod:`patchfrog.contract_intelligence.base_fetch`).
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from patchfrog.change_intelligence.affected_surface import derive_affected_surface
from patchfrog.change_intelligence.domain import ChangeUnit
from patchfrog.contract_intelligence.base_fetch import fetch_base_file_contents, parse_base_symbols
from patchfrog.contract_intelligence.boundaries import (
    find_contract_eligible_candidates,
    needed_base_file_paths,
)
from patchfrog.contract_intelligence.delta import diff_signatures
from patchfrog.contract_intelligence.domain import (
    CONTRACT_INTELLIGENCE_VERSION,
    MAX_CANDIDATES_CONSIDERED,
    ContractDelta,
    ContractDescriptor,
    ContractIntelligenceReport,
    ContractKind,
)
from patchfrog.contract_intelligence.function_signature import parse_python_signature
from patchfrog.contract_intelligence.stale_consumers import derive_stale_consumers
from patchfrog.contract_intelligence.story import build_contract_story
from patchfrog.intelligence.queries import RepositoryQueryService
from patchfrog.review.domain import ReviewCandidate

_EMPTY_REPORT = ContractIntelligenceReport(
    version=CONTRACT_INTELLIGENCE_VERSION, descriptors=(), deltas=(), stale_consumers=(), contract_story=""
)


async def build_contract_intelligence_report(
    session: AsyncSession,
    *,
    candidates: list[ReviewCandidate],
    change_units: tuple[ChangeUnit, ...] = (),
    base_sha: str | None,
    local: bool = False,
    root_path: Path | None = None,
    clone_url: str | None = None,
    token: str | None = None,
    query_service: RepositoryQueryService | None = None,
) -> ContractIntelligenceReport:
    if base_sha is None:
        return _EMPTY_REPORT

    queries = query_service or RepositoryQueryService()
    bounded_candidates = tuple(candidates[:MAX_CANDIDATES_CONSIDERED])
    eligible = await find_contract_eligible_candidates(session, candidates=bounded_candidates, query_service=queries)
    if not eligible:
        return _EMPTY_REPORT

    file_contents = fetch_base_file_contents(
        local=local, base_sha=base_sha, paths=needed_base_file_paths(eligible),
        root_path=root_path, clone_url=clone_url, token=token,
    )
    base_symbols_by_file = parse_base_symbols(file_contents=file_contents)

    all_changed_symbol_ids = {c.symbol_id for c in bounded_candidates if c.symbol_id is not None}
    unit_by_symbol_id: dict[UUID, str] = {}
    for unit in change_units:
        for c in unit.changed_candidates:
            if c.symbol_id is not None:
                unit_by_symbol_id[c.symbol_id] = unit.id

    descriptors: list[ContractDescriptor] = []
    deltas: list[ContractDelta] = []
    contract_symbol_ids: dict[str, UUID] = {}

    for e in eligible:
        contract_id = _contract_id(e.candidate.file_path, e.candidate.qualified_name or "")
        contract_symbol_ids[contract_id] = e.symbol.id
        descriptors.append(
            ContractDescriptor(
                id=contract_id,
                kind=ContractKind.FUNCTION,
                qualified_name=e.candidate.qualified_name or "",
                file_path=e.candidate.file_path,
                externally_consumed=True,
                normalized_shape=e.symbol.signature or "",
                evidence=f"{e.caller_count} real resolved caller(s)",
            )
        )

        base_symbol = base_symbols_by_file.get(e.candidate.file_path, {}).get(e.candidate.qualified_name or "")
        if base_symbol is None or base_symbol.signature is None or e.symbol.signature is None:
            continue  # newly introduced at head, or base content unavailable -- nothing to diff
        if base_symbol.signature.strip() == e.symbol.signature.strip():
            continue  # byte-identical header -- no real change

        base_sig = parse_python_signature(base_symbol.signature)
        head_sig = parse_python_signature(e.symbol.signature)
        if base_sig is None or head_sig is None:
            continue  # fail closed -- never guess from an unparseable header

        characteristics = diff_signatures(base_sig, head_sig)
        if not characteristics:
            continue  # text differs (e.g. cosmetic) but no structural delta

        blast_radius = await derive_affected_surface(
            session, changed_candidates=(e.candidate,), query_service=queries
        )
        deltas.append(
            ContractDelta(
                contract_id=contract_id,
                qualified_name=e.candidate.qualified_name or "",
                file_path=e.candidate.file_path,
                kind=ContractKind.FUNCTION,
                change_unit_id=unit_by_symbol_id.get(e.symbol.id),
                before_signature=base_symbol.signature,
                after_signature=e.symbol.signature,
                characteristics=characteristics,
                evidence=(
                    f"base signature {base_symbol.signature.strip()!r} -> "
                    f"head signature {e.symbol.signature.strip()!r}"
                ),
                blast_radius=blast_radius,
            )
        )

    stale_consumers = await derive_stale_consumers(
        session,
        deltas=tuple(deltas),
        all_changed_symbol_ids=all_changed_symbol_ids,
        contract_symbol_ids=contract_symbol_ids,
        query_service=queries,
    )
    contract_story = build_contract_story(tuple(deltas), stale_consumers)

    return ContractIntelligenceReport(
        version=CONTRACT_INTELLIGENCE_VERSION,
        descriptors=tuple(descriptors),
        deltas=tuple(deltas),
        stale_consumers=stale_consumers,
        contract_story=contract_story,
    )


def _contract_id(file_path: str, qualified_name: str) -> str:
    canonical = f"{file_path}\x1f{qualified_name}"
    return hashlib.sha256(canonical.encode()).hexdigest()[:16]
