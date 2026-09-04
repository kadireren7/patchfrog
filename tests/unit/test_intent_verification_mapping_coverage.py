"""Unit tests for :mod:`patchfrog.intent_verification.mapping`/
:mod:`patchfrog.intent_verification.coverage` -- deterministic lexical
mapping (spec section 8) and evidence-backed gap derivation (spec
sections 9/11/14). Pure/synchronous, no database, no LLM -- every case
is a hand-built :class:`~patchfrog.change_intelligence.domain.ChangeUnit`.
"""

from __future__ import annotations

import uuid

from patchfrog.change_intelligence.domain import (
    AffectedRelation,
    AffectedSymbolRef,
    ChangeKind,
    ChangeUnit,
    CompanionReasonCode,
    CompanionStatus,
    ExpectedCompanionChange,
)
from patchfrog.intent_verification.coverage import derive_coverage_and_gaps
from patchfrog.intent_verification.domain import IntentCoverageStatus, IntentGapReasonCode
from patchfrog.intent_verification.extraction import extract_claims_from_pr_metadata
from patchfrog.intent_verification.mapping import map_claim_to_units
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


def _claim(title: str):  # type: ignore[no-untyped-def]
    claims = extract_claims_from_pr_metadata(title=title, body=None)
    assert claims
    return claims[0]


def test_unrelated_unit_never_mapped() -> None:
    """Spec section 8/corpus case 9: unrelated ChangeUnits are never mapped."""

    claim = _claim("Prevent duplicate webhook payment processing")
    unrelated = ChangeUnit(
        id="u1", title="update README formatting", change_kind=ChangeKind.BEHAVIOR,
        changed_candidates=(_candidate(file_path="docs/readme_helper.py", qualified_name="format_heading"),),
    )
    mapped = map_claim_to_units(claim, (unrelated,))
    assert mapped == ()


def test_related_unit_mapped_via_shared_terms() -> None:
    claim = _claim("Prevent duplicate webhook payment processing")
    unit = ChangeUnit(
        id="u1", title="payment idempotency", change_kind=ChangeKind.BEHAVIOR,
        changed_candidates=(_candidate(file_path="service.py", qualified_name="process_payment"),),
    )
    mapped = map_claim_to_units(claim, (unit,))
    assert len(mapped) == 1
    assert mapped[0][0] is unit
    assert "payment" in mapped[0][1]


def test_mapping_bounded_to_max_units() -> None:
    from patchfrog.intent_verification.domain import MAX_MAPPED_UNITS_PER_CLAIM

    claim = _claim("Prevent duplicate webhook payment processing")
    units = tuple(
        ChangeUnit(
            id=f"u{i}", title=f"payment webhook unit {i}", change_kind=ChangeKind.BEHAVIOR,
            changed_candidates=(_candidate(file_path=f"s{i}.py", qualified_name=f"process_payment_{i}"),),
        )
        for i in range(5)
    )
    mapped = map_claim_to_units(claim, units)
    assert len(mapped) <= MAX_MAPPED_UNITS_PER_CLAIM


def test_coverage_supported_when_no_gap() -> None:
    claim = _claim("Prevent duplicate webhook payment processing")
    unit = ChangeUnit(
        id="u1", title="payment idempotency", change_kind=ChangeKind.BEHAVIOR,
        changed_candidates=(_candidate(file_path="service.py", qualified_name="process_payment"),),
    )
    mapped = map_claim_to_units(claim, (unit,))
    coverage, gaps = derive_coverage_and_gaps(claim, mapped, contract_deltas=(), expected_companions=())
    assert coverage.status is IntentCoverageStatus.SUPPORTED
    assert gaps == ()
    assert coverage.covered_surfaces == ("process_payment",)


def test_coverage_insufficient_when_unmapped() -> None:
    claim = _claim("Prevent duplicate webhook payment processing")
    coverage, gaps = derive_coverage_and_gaps(claim, (), contract_deltas=(), expected_companions=())
    assert coverage.status is IntentCoverageStatus.INSUFFICIENT_EVIDENCE
    assert gaps == ()


def test_partial_fulfillment_gap_detected() -> None:
    """Spec section 13's highest-value target: a real, relevant affected
    surface that wasn't changed produces a PotentialIntentGap and
    PARTIAL_EVIDENCE status."""

    claim = _claim("Prevent duplicate retry payment processing")
    unit = ChangeUnit(
        id="u1", title="retry payment idempotency", change_kind=ChangeKind.BEHAVIOR,
        changed_candidates=(_candidate(file_path="service.py", qualified_name="process_payment"),),
        affected_surface=(_affected(file_path="retry_worker.py", qualified_name="RetryWorker.run"),),
    )
    mapped = map_claim_to_units(claim, (unit,))
    coverage, gaps = derive_coverage_and_gaps(claim, mapped, contract_deltas=(), expected_companions=())
    assert coverage.status is IntentCoverageStatus.PARTIAL_EVIDENCE
    assert len(gaps) == 1
    assert gaps[0].expected_surface.qualified_name == "RetryWorker.run"
    assert gaps[0].reason_code is IntentGapReasonCode.EXPECTED_SURFACE_UNCHANGED


def test_irrelevant_affected_surface_never_becomes_a_gap() -> None:
    """A real affected-surface node exists, but shares no term with the
    claim -- never fabricated into a gap (spec section 8: "If mapping is
    ambiguous: leave the claim unmapped" applies at the surface level
    too)."""

    claim = _claim("Prevent duplicate webhook payment processing")
    unit = ChangeUnit(
        id="u1", title="payment idempotency", change_kind=ChangeKind.BEHAVIOR,
        changed_candidates=(_candidate(file_path="service.py", qualified_name="process_payment"),),
        affected_surface=(_affected(file_path="logging_utils.py", qualified_name="format_log_line"),),
    )
    mapped = map_claim_to_units(claim, (unit,))
    coverage, gaps = derive_coverage_and_gaps(claim, mapped, contract_deltas=(), expected_companions=())
    assert gaps == ()
    assert coverage.status is IntentCoverageStatus.SUPPORTED


def test_missing_companion_dedup_not_a_second_gap_object() -> None:
    """Spec section 14: an existing MISSING ExpectedCompanionChange
    (already produced by Change/Contract Intelligence) is referenced via
    ``relevant_companion_candidates``, never duplicated as a second
    PotentialIntentGap."""

    claim = _claim("Prevent duplicate webhook payment processing")
    unit = ChangeUnit(
        id="u1", title="payment idempotency", change_kind=ChangeKind.BEHAVIOR,
        changed_candidates=(_candidate(file_path="service.py", qualified_name="process_payment"),),
    )
    missing_companion = ExpectedCompanionChange(
        change_unit_id="u1", source_qualified_name="process_payment", source_file_path="service.py",
        expected_qualified_name="test_process_payment", expected_file_path="test_service.py",
        reason_code=CompanionReasonCode.TEST_NOT_UPDATED, reason="likely test not updated",
        evidence="file_tests_file edge", status=CompanionStatus.MISSING,
    )
    mapped = map_claim_to_units(claim, (unit,))
    coverage, gaps = derive_coverage_and_gaps(
        claim, mapped, contract_deltas=(), expected_companions=(missing_companion,)
    )
    assert coverage.status is IntentCoverageStatus.PARTIAL_EVIDENCE
    assert gaps == ()  # no duplicate PotentialIntentGap constructed
    assert coverage.relevant_companion_candidates == (missing_companion,)


def test_observed_companion_never_flips_status_to_partial() -> None:
    claim = _claim("Prevent duplicate webhook payment processing")
    unit = ChangeUnit(
        id="u1", title="payment idempotency", change_kind=ChangeKind.BEHAVIOR,
        changed_candidates=(_candidate(file_path="service.py", qualified_name="process_payment"),),
    )
    observed_companion = ExpectedCompanionChange(
        change_unit_id="u1", source_qualified_name="process_payment", source_file_path="service.py",
        expected_qualified_name="handle_payment", expected_file_path="caller.py",
        reason_code=CompanionReasonCode.CALLER_NOT_UPDATED, reason="already updated",
        evidence="call edge", status=CompanionStatus.OBSERVED,
    )
    mapped = map_claim_to_units(claim, (unit,))
    coverage, gaps = derive_coverage_and_gaps(
        claim, mapped, contract_deltas=(), expected_companions=(observed_companion,)
    )
    assert coverage.status is IntentCoverageStatus.SUPPORTED
    assert gaps == ()
