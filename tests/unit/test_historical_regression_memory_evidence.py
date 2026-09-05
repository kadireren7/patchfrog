"""Unit tests for :mod:`patchfrog.historical_regression_memory.evidence`
-- bounded, per-candidate evidence text, empty for every candidate with
no historical match of its own (Quality + Cost Guard budget
discipline)."""

from __future__ import annotations

import uuid

from patchfrog.analysis.domain import FindingCategory
from patchfrog.historical_regression_memory.domain import (
    HISTORICAL_REGRESSION_MEMORY_VERSION,
    HistoricalEvidenceStrength,
    HistoricalMatchKind,
    HistoricalRegressionReasonCode,
    HistoricalRegressionRecord,
    HistoricalRegressionReport,
    PotentialHistoricalRegression,
)
from patchfrog.historical_regression_memory.evidence import evidence_text_for_candidate
from patchfrog.review.domain import ReviewCandidate, ReviewCandidateReason


def _candidate(*, file_path: str, qualified_name: str | None) -> ReviewCandidate:
    return ReviewCandidate(
        file_path=file_path, symbol_id=uuid.uuid4() if qualified_name else None,
        symbol_name=qualified_name.rsplit(".", 1)[-1] if qualified_name else None,
        qualified_name=qualified_name, start_line=1, end_line=5, changed_lines=(1,),
        static_finding_ids=(), reason=ReviewCandidateReason.CHANGED_SYMBOL,
    )


def _report_with_match(*, file_path: str, qualified_name: str) -> HistoricalRegressionReport:
    record = HistoricalRegressionRecord(
        historical_finding_id=uuid.uuid4(), repository_id=uuid.uuid4(), historical_review_run_id=uuid.uuid4(),
        historical_commit_sha="a" * 40, source_file_path=file_path, source_qualified_name=qualified_name,
        finding_category=FindingCategory.CORRECTNESS, evidence_strength=HistoricalEvidenceStrength.CONFIRMED_FIXED,
        bounded_evidence_fingerprint="forgot idempotency key", observed_at="2026-01-01T00:00:00+00:00",
    )
    candidate = PotentialHistoricalRegression(
        current_change_unit_id="u1", current_file_path=file_path, current_qualified_name=qualified_name,
        historical_record=record, match_kind=HistoricalMatchKind.SAME_SYMBOL,
        reason_code=HistoricalRegressionReasonCode.PREVIOUS_FIXED_FINDING_SAME_SYMBOL, evidence="matched",
    )
    return HistoricalRegressionReport(
        version=HISTORICAL_REGRESSION_MEMORY_VERSION, trusted_records_considered=(record,),
        candidates=(candidate,), historical_story="",
    )


def test_empty_for_candidate_with_no_match() -> None:
    report = _report_with_match(file_path="service.py", qualified_name="process_payment")
    unrelated = _candidate(file_path="other.py", qualified_name="other_fn")
    assert evidence_text_for_candidate(report, unrelated) == ""


def test_populated_for_the_exact_match_candidate() -> None:
    report = _report_with_match(file_path="service.py", qualified_name="process_payment")
    candidate = _candidate(file_path="service.py", qualified_name="process_payment")
    text = evidence_text_for_candidate(report, candidate)
    assert "forgot idempotency key" in text
    assert "confirmed_fixed" in text
    assert "same_symbol" in text
    assert "previous_fixed_finding_same_symbol" in text


def test_empty_for_different_symbol_in_same_file() -> None:
    report = _report_with_match(file_path="service.py", qualified_name="process_payment")
    candidate = _candidate(file_path="service.py", qualified_name="unrelated_fn")
    assert evidence_text_for_candidate(report, candidate) == ""


def test_empty_report_never_crashes() -> None:
    report = HistoricalRegressionReport(
        version=HISTORICAL_REGRESSION_MEMORY_VERSION, trusted_records_considered=(), candidates=(),
        historical_story="",
    )
    candidate = _candidate(file_path="service.py", qualified_name="process_payment")
    assert evidence_text_for_candidate(report, candidate) == ""
