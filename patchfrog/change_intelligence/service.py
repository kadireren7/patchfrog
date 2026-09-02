"""Top-level Change Intelligence orchestrator.

:func:`build_change_intelligence_report` is the one entry point
everything else in this package composes into -- given a review run's
already-generated, diff-driven
:class:`~patchfrog.review.domain.ReviewCandidate` list, it groups them
into :class:`~patchfrog.change_intelligence.domain.ChangeUnit`\\ s,
derives each unit's affected surface and expected companion changes,
derives attention areas, and produces a deterministic Change Story plus
an optional, conditional Change Map.

Called exactly once per review run (never per-candidate) -- see
:mod:`patchfrog.review.service`'s integration point. Zero LLM calls,
zero mutation beyond the caller's own eventual persistence of the
resulting summary counts/text (see
:func:`patchfrog.change_intelligence.telemetry.summarize_for_persistence`).
"""

from __future__ import annotations

import hashlib

from sqlalchemy.ext.asyncio import AsyncSession

from patchfrog.change_intelligence.affected_surface import derive_affected_surface
from patchfrog.change_intelligence.attention import (
    attach_missing_companion_signal,
    derive_attention_areas,
)
from patchfrog.change_intelligence.change_kind import classify_candidate, combine_kinds
from patchfrog.change_intelligence.change_map import render_change_map, select_change_map_unit
from patchfrog.change_intelligence.change_story import build_change_story
from patchfrog.change_intelligence.companions import derive_expected_companions
from patchfrog.change_intelligence.domain import (
    CHANGE_INTELLIGENCE_VERSION,
    ChangeIntelligenceReport,
    ChangeKind,
    ChangeUnit,
    CompanionStatus,
    ExpectedCompanionChange,
)
from patchfrog.change_intelligence.grouping import group_into_change_units
from patchfrog.intelligence.queries import RepositoryQueryService
from patchfrog.review.domain import ReviewCandidate

#: A report with more changed candidates than this is almost certainly
#: an oversized PR already rejected upstream by MAX_CHANGED_FILES/
#: MAX_DIFF_BYTES (patchfrog.ops.eligibility) or capped by
#: ReviewConfig.max_candidates -- this is a final, defensive bound so
#: Change Intelligence itself never does unbounded work even if called
#: with an unexpectedly large candidate list directly (e.g. from a test
#: or the CLI).
MAX_CANDIDATES_CONSIDERED = 150


async def build_change_intelligence_report(
    session: AsyncSession,
    *,
    candidates: list[ReviewCandidate],
    query_service: RepositoryQueryService | None = None,
) -> ChangeIntelligenceReport:
    queries = query_service or RepositoryQueryService()
    bounded_candidates = candidates[:MAX_CANDIDATES_CONSIDERED]

    if not bounded_candidates:
        return ChangeIntelligenceReport(
            version=CHANGE_INTELLIGENCE_VERSION,
            change_units=(),
            expected_companions=(),
            attention_areas=(),
            change_story="",
            change_map=None,
        )

    grouped = await group_into_change_units(session, candidates=bounded_candidates, query_service=queries)

    all_changed_symbol_ids = {c.symbol_id for c in bounded_candidates if c.symbol_id is not None}
    all_changed_file_paths = {c.file_path for c in bounded_candidates}

    units: list[ChangeUnit] = []
    all_companions: list[ExpectedCompanionChange] = []
    for member_candidates in grouped:
        unit_id = _unit_id(member_candidates)
        kinds = [await _classify(session, queries, c) for c in member_candidates]
        change_kind = combine_kinds(kinds)
        affected_surface = await derive_affected_surface(
            session, changed_candidates=member_candidates, query_service=queries
        )
        unit = ChangeUnit(
            id=unit_id,
            title=_unit_title(member_candidates),
            change_kind=change_kind,
            changed_candidates=member_candidates,
            affected_surface=affected_surface,
        )
        units.append(unit)

        companions = await derive_expected_companions(
            session,
            change_unit_id=unit_id,
            changed_candidates=member_candidates,
            all_changed_symbol_ids=all_changed_symbol_ids,
            all_changed_file_paths=all_changed_file_paths,
            query_service=queries,
        )
        all_companions.extend(companions)

    attention_areas = derive_attention_areas(tuple(units))
    missing_by_unit: dict[str, int] = {}
    for companion in all_companions:
        if companion.status is CompanionStatus.MISSING:
            missing_by_unit[companion.change_unit_id] = missing_by_unit.get(companion.change_unit_id, 0) + 1
    for unit_id_key, missing_count in missing_by_unit.items():
        attention_areas = attach_missing_companion_signal(
            attention_areas, change_unit_id=unit_id_key, missing_count=missing_count
        )

    change_story = build_change_story(tuple(units), tuple(all_companions))

    change_map = None
    map_unit = select_change_map_unit(tuple(units))
    if map_unit is not None:
        change_map = render_change_map(map_unit, expected_companions=tuple(all_companions))

    return ChangeIntelligenceReport(
        version=CHANGE_INTELLIGENCE_VERSION,
        change_units=tuple(units),
        expected_companions=tuple(all_companions),
        attention_areas=attention_areas,
        change_story=change_story,
        change_map=change_map,
    )


async def _classify(session: AsyncSession, queries: RepositoryQueryService, candidate: ReviewCandidate) -> ChangeKind:
    is_test = False
    has_cross_file_caller = False
    if candidate.symbol_id is not None:
        symbol = await queries.get_symbol_by_id(session, symbol_id=candidate.symbol_id)
        if symbol is not None:
            file = await queries.get_file_by_id(session, indexed_file_id=symbol.indexed_file_id)
            if file is not None:
                is_test = file.is_test
            callers = await queries.get_callers(session, symbol_id=candidate.symbol_id)
            for ref in callers:
                if ref.caller_symbol_id is None:
                    continue
                caller_symbol = await queries.get_symbol_by_id(session, symbol_id=ref.caller_symbol_id)
                if caller_symbol is not None:
                    caller_file = await queries.get_file_by_id(session, indexed_file_id=caller_symbol.indexed_file_id)
                    if caller_file is not None and caller_file.relative_path != candidate.file_path:
                        has_cross_file_caller = True
                        break
    return classify_candidate(file_path=candidate.file_path, is_test=is_test, has_cross_file_caller=has_cross_file_caller)


def _unit_id(members: tuple[ReviewCandidate, ...]) -> str:
    canonical = "\x1f".join(sorted(c.fingerprint() for c in members))
    return hashlib.sha256(canonical.encode()).hexdigest()[:16]


def _unit_title(members: tuple[ReviewCandidate, ...]) -> str:
    labels = [c.qualified_name or c.symbol_name or c.file_path for c in members[:3]]
    title = ", ".join(labels)
    if len(members) > 3:
        title += f" (+{len(members) - 3} more)"
    return title
