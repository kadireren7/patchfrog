"""Deterministic derivation of :class:`~patchfrog.historical_regression_memory.domain.PotentialHistoricalRegression`\\ s
-- pure, synchronous, consuming only already-fetched
:class:`~patchfrog.historical_regression_memory.domain.HistoricalRegressionRecord`\\ s
and already-computed J/K/L current-run objects. No repository-graph
query, no base-commit fetch, zero LLM calls.

See ``validation/historical_regression_memory/latest-summary.md``
sections 4-7 for the full design narrative behind the current-surface
pool, the match-kind hierarchy, and the dedup-ownership rules
implemented here.
"""

from __future__ import annotations

from dataclasses import dataclass

from patchfrog.change_intelligence.domain import (
    ChangeKind,
    ChangeUnit,
    CompanionStatus,
    ExpectedCompanionChange,
)
from patchfrog.contract_intelligence.domain import ContractDelta
from patchfrog.historical_regression_memory.domain import (
    MAX_HISTORICAL_RECORDS_PER_SURFACE,
    MAX_HISTORICAL_REGRESSION_CANDIDATES,
    HistoricalEvidenceStrength,
    HistoricalMatchKind,
    HistoricalRegressionReasonCode,
    HistoricalRegressionRecord,
    PotentialHistoricalRegression,
)
from patchfrog.intent_verification.domain import PotentialIntentGap
from patchfrog.test_intelligence.domain import PotentialTestGap

_REASON_BY_MATCH_AND_TRUST: dict[
    tuple[HistoricalMatchKind, HistoricalEvidenceStrength], HistoricalRegressionReasonCode
] = {
    (HistoricalMatchKind.SAME_SYMBOL, HistoricalEvidenceStrength.CONFIRMED_FIXED):
        HistoricalRegressionReasonCode.PREVIOUS_FIXED_FINDING_SAME_SYMBOL,
    (HistoricalMatchKind.SAME_SYMBOL, HistoricalEvidenceStrength.CONFIRMED_USEFUL):
        HistoricalRegressionReasonCode.PREVIOUS_USEFUL_FINDING_SAME_SYMBOL,
    (HistoricalMatchKind.SAME_QUALIFIED_NAME_IN_SAME_FILE, HistoricalEvidenceStrength.CONFIRMED_FIXED):
        HistoricalRegressionReasonCode.PREVIOUS_FIXED_FINDING_SAME_SYMBOL,
    (HistoricalMatchKind.SAME_QUALIFIED_NAME_IN_SAME_FILE, HistoricalEvidenceStrength.CONFIRMED_USEFUL):
        HistoricalRegressionReasonCode.PREVIOUS_USEFUL_FINDING_SAME_SYMBOL,
    # SAME_FILE is deliberately absent -- see _match_kind_for's own
    # docstring: it is never constructed in v1, so no reason-code
    # mapping is needed for it. PREVIOUS_FIXED_FINDING_SAME_FILE stays
    # defined on the enum for forward documentation only.
    (HistoricalMatchKind.GRAPH_RELATED_SURFACE, HistoricalEvidenceStrength.CONFIRMED_FIXED):
        HistoricalRegressionReasonCode.PREVIOUS_REGRESSION_RELATED_SURFACE,
    (HistoricalMatchKind.GRAPH_RELATED_SURFACE, HistoricalEvidenceStrength.CONFIRMED_USEFUL):
        HistoricalRegressionReasonCode.PREVIOUS_REGRESSION_RELATED_SURFACE,
}


@dataclass(frozen=True, slots=True)
class _PoolEntry:
    change_unit_id: str
    file_path: str
    qualified_name: str
    is_directly_changed: bool


def _build_surface_pool(
    *,
    change_units: tuple[ChangeUnit, ...],
    contract_deltas: tuple[ContractDelta, ...],
    intent_gaps: tuple[PotentialIntentGap, ...],
) -> tuple[_PoolEntry, ...]:
    """See the audit's "Current surface pool" section: exactly four
    reused sources, never a new graph traversal. Deduplicated by
    ``(change_unit_id, file_path, qualified_name)`` -- the first entry
    to claim a key wins ``is_directly_changed`` (a symbol directly
    changed in one unit always outranks a merely-affected mention of
    the same symbol reached via another path)."""

    pool: dict[tuple[str, str, str], _PoolEntry] = {}

    def _add(change_unit_id: str, file_path: str, qualified_name: str | None, is_directly_changed: bool) -> None:
        if qualified_name is None:
            return
        key = (change_unit_id, file_path, qualified_name)
        existing = pool.get(key)
        if existing is not None and existing.is_directly_changed:
            return
        pool[key] = _PoolEntry(
            change_unit_id=change_unit_id, file_path=file_path, qualified_name=qualified_name,
            is_directly_changed=is_directly_changed,
        )

    for unit in change_units:
        for candidate in unit.changed_candidates:
            _add(unit.id, candidate.file_path, candidate.qualified_name, True)
        for ref in unit.affected_surface:
            _add(unit.id, ref.file_path, ref.qualified_name, False)

    unit_id_by_symbol: dict[tuple[str, str], str] = {
        (e.file_path, e.qualified_name): e.change_unit_id for e in pool.values() if e.is_directly_changed
    }

    for delta in contract_deltas:
        change_unit_id = delta.change_unit_id or unit_id_by_symbol.get((delta.file_path, delta.qualified_name), "")
        if not change_unit_id:
            continue
        for ref in delta.blast_radius:
            _add(change_unit_id, ref.file_path, ref.qualified_name, False)

    for gap in intent_gaps:
        _add(gap.change_unit_id, gap.expected_surface.file_path, gap.expected_surface.qualified_name, False)

    return tuple(pool.values())


def _match_kind_for(
    record: HistoricalRegressionRecord, *, pool: tuple[_PoolEntry, ...], directly_changed_files: frozenset[str]
) -> tuple[HistoricalMatchKind, _PoolEntry] | None:
    """``SAME_FILE`` is deliberately never constructed here (spec's own
    correction: "same file alone is weak... require additional
    deterministic relevance evidence; otherwise defer" -- no such
    evidence can be expressed here beyond what
    ``SAME_QUALIFIED_NAME_IN_SAME_FILE``/``GRAPH_RELATED_SURFACE``
    already require, so the safe choice is precision over taxonomy
    coverage: v1 never emits a bare same-file candidate). The
    ``HistoricalMatchKind.SAME_FILE``/``HistoricalRegressionReasonCode.PREVIOUS_FIXED_FINDING_SAME_FILE``
    enum members are kept for forward documentation only -- see
    ``validation/historical_regression_memory/latest-summary.md``
    section 2 for the full correction narrative. A real match always
    requires an *exact* ``(file_path, qualified_name)`` hit against the
    current surface pool -- a file matching alone, with no symbol
    identity confirmed, is never enough."""

    if record.source_qualified_name is None:
        return None

    exact = [
        e for e in pool if e.file_path == record.source_file_path and e.qualified_name == record.source_qualified_name
    ]
    if not exact:
        return None

    entry = exact[0]
    if entry.is_directly_changed:
        return HistoricalMatchKind.SAME_SYMBOL, entry
    if entry.file_path in directly_changed_files:
        return HistoricalMatchKind.SAME_QUALIFIED_NAME_IN_SAME_FILE, entry
    return HistoricalMatchKind.GRAPH_RELATED_SURFACE, entry


def _enrichment_for(
    entry: _PoolEntry,
    *,
    expected_companions: tuple[ExpectedCompanionChange, ...],
    intent_gaps: tuple[PotentialIntentGap, ...],
    test_gaps: tuple[PotentialTestGap, ...],
) -> tuple[ExpectedCompanionChange | None, PotentialIntentGap | None, PotentialTestGap | None]:
    """Dedup ownership (audit section 7): when the matched surface is
    already a real J/K ``MISSING`` companion, an L intent gap, or an M
    test gap on the same ``(file_path, qualified_name)``, reference it
    -- never construct a second, competing top-level candidate."""

    for companion in expected_companions:
        if companion.status is not CompanionStatus.MISSING:
            continue
        if companion.expected_file_path == entry.file_path and companion.expected_qualified_name == entry.qualified_name:
            return companion, None, None

    for gap in intent_gaps:
        if gap.expected_surface.file_path == entry.file_path and gap.expected_surface.qualified_name == entry.qualified_name:
            return None, gap, None

    for test_gap in test_gaps:
        if (
            test_gap.expectation.source_file_path == entry.file_path
            and test_gap.expectation.source_qualified_name == entry.qualified_name
        ):
            return None, None, test_gap

    return None, None, None


def derive_historical_regression_candidates(
    *,
    trusted_records: tuple[HistoricalRegressionRecord, ...],
    change_units: tuple[ChangeUnit, ...],
    contract_deltas: tuple[ContractDelta, ...] = (),
    intent_gaps: tuple[PotentialIntentGap, ...] = (),
    test_gaps: tuple[PotentialTestGap, ...] = (),
    expected_companions: tuple[ExpectedCompanionChange, ...] = (),
) -> tuple[PotentialHistoricalRegression, ...]:
    # A TEST-kind ChangeUnit (every one of its own candidates lives in a
    # test file, e.g. classify_candidate/combine_kinds) contributes
    # nothing to the surface pool at all -- otherwise a test file that
    # merely *calls* a historically-risky production symbol (a real,
    # legitimate J call edge) would surface a production regression
    # candidate for a test-only PR, exactly the "not an inverse feature
    # detector" failure mode M's own corpus caught. Mirrors M's own
    # BEHAVIOR-only conservatism for NO_TEST_SURFACE_FOUND.
    non_test_units = tuple(u for u in change_units if u.change_kind is not ChangeKind.TEST)
    pool = _build_surface_pool(change_units=non_test_units, contract_deltas=contract_deltas, intent_gaps=intent_gaps)
    directly_changed_files = frozenset(
        candidate.file_path for unit in non_test_units for candidate in unit.changed_candidates
    )

    per_surface_counts: dict[tuple[str, str], int] = {}
    out: list[PotentialHistoricalRegression] = []

    for record in trusted_records:
        matched = _match_kind_for(record, pool=pool, directly_changed_files=directly_changed_files)
        if matched is None:
            continue
        match_kind, entry = matched

        reason_code = _REASON_BY_MATCH_AND_TRUST.get((match_kind, record.evidence_strength))
        if reason_code is None:
            continue  # e.g. SAME_FILE + CONFIRMED_USEFUL -- deliberately not implemented (too weak)

        surface_key = (record.source_file_path, record.source_qualified_name or "")
        count = per_surface_counts.get(surface_key, 0)
        if count >= MAX_HISTORICAL_RECORDS_PER_SURFACE:
            continue
        per_surface_counts[surface_key] = count + 1

        enriches_companion, enriches_intent_gap, enriches_test_gap = _enrichment_for(
            entry, expected_companions=expected_companions, intent_gaps=intent_gaps, test_gaps=test_gaps
        )

        label = record.source_qualified_name or record.source_file_path
        evidence = (
            f"a previous {record.evidence_strength.value.replace('confirmed_', '')} finding "
            f"({record.bounded_evidence_fingerprint!r}) involved {label!r}"
        )

        out.append(
            PotentialHistoricalRegression(
                current_change_unit_id=entry.change_unit_id,
                current_file_path=entry.file_path,
                current_qualified_name=entry.qualified_name,
                historical_record=record,
                match_kind=match_kind,
                reason_code=reason_code,
                evidence=evidence,
                enriches_companion=enriches_companion,
                enriches_intent_gap=enriches_intent_gap,
                enriches_test_gap=enriches_test_gap,
            )
        )
        if len(out) >= MAX_HISTORICAL_REGRESSION_CANDIDATES:
            break

    return tuple(out)
