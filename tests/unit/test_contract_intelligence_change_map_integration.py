"""Proves the exact merge behavior
:mod:`patchfrog.review.service` performs (spec section 10): a contract
stale-consumer candidate (``ExpectedCompanionChange`` with reason code
``CONTRACT_CONSUMER_NOT_UPDATED``), when included alongside Change
Intelligence's own companions and passed to the *existing*
``render_change_map``, renders under "Expected but missing" with zero
new rendering code. Pure/synchronous -- no database, no LLM."""

from __future__ import annotations

import uuid

from patchfrog.change_intelligence.change_map import render_change_map, select_change_map_unit
from patchfrog.change_intelligence.domain import (
    AffectedRelation,
    AffectedSymbolRef,
    ChangeKind,
    ChangeUnit,
    CompanionReasonCode,
    CompanionStatus,
    ExpectedCompanionChange,
)
from patchfrog.review.domain import ReviewCandidate, ReviewCandidateReason


def _candidate(*, file_path: str, qualified_name: str) -> ReviewCandidate:
    return ReviewCandidate(
        file_path=file_path, symbol_id=uuid.uuid4(), symbol_name=qualified_name.rsplit(".", 1)[-1],
        qualified_name=qualified_name, start_line=1, end_line=5, changed_lines=(1,),
        static_finding_ids=(), reason=ReviewCandidateReason.CHANGED_SYMBOL,
    )


def _affected(*, file_path: str, qualified_name: str) -> AffectedSymbolRef:
    return AffectedSymbolRef(
        file_path=file_path, qualified_name=qualified_name, symbol_name=qualified_name.rsplit(".", 1)[-1],
        relation=AffectedRelation.DIRECTLY_DEPENDENT, distance=1, reason="directly calls the changed symbol",
    )


def test_contract_stale_consumer_renders_under_expected_but_missing() -> None:
    unit = ChangeUnit(
        id="unit1", title="save", change_kind=ChangeKind.CONTRACT,
        changed_candidates=(_candidate(file_path="repository.py", qualified_name="save"),),
        affected_surface=(
            _affected(file_path="service.py", qualified_name="process"),
            _affected(file_path="caller.py", qualified_name="handle"),
        ),
    )
    assert select_change_map_unit((unit,)) is unit  # 3 nodes across 2 files -- eligible

    contract_companion = ExpectedCompanionChange(
        change_unit_id="unit1",
        source_qualified_name="save",
        source_file_path="repository.py",
        expected_qualified_name="process",
        expected_file_path="service.py",
        reason_code=CompanionReasonCode.CONTRACT_CONSUMER_NOT_UPDATED,
        reason="'process' calls 'save', whose contract changed -- it may still assume the old shape",
        evidence="call edge: process -> save; base signature ... -> head signature ...",
        status=CompanionStatus.MISSING,
    )

    change_map = render_change_map(unit, expected_companions=(contract_companion,))

    assert "Expected but missing:" in change_map.text
    assert "process" in change_map.text
    assert "may still assume the old shape" in change_map.text


def test_contract_observed_companion_never_appears_as_missing() -> None:
    unit = ChangeUnit(
        id="unit1", title="save", change_kind=ChangeKind.CONTRACT,
        changed_candidates=(_candidate(file_path="repository.py", qualified_name="save"),),
        affected_surface=(
            _affected(file_path="service.py", qualified_name="process"),
            _affected(file_path="caller.py", qualified_name="handle"),
        ),
    )
    observed = ExpectedCompanionChange(
        change_unit_id="unit1",
        source_qualified_name="save",
        source_file_path="repository.py",
        expected_qualified_name="process",
        expected_file_path="service.py",
        reason_code=CompanionReasonCode.CONTRACT_CONSUMER_NOT_UPDATED,
        reason="already updated",
        evidence="call edge: process -> save",
        status=CompanionStatus.OBSERVED,
    )
    change_map = render_change_map(unit, expected_companions=(observed,))
    assert "Expected but missing:" not in change_map.text
