"""Unit tests for :mod:`patchfrog.test_intelligence.evidence` -- bounded,
per-candidate evidence text, empty for every candidate with no gap of
its own (Quality + Cost Guard budget discipline)."""

from __future__ import annotations

import uuid

from patchfrog.change_intelligence.domain import CompanionStatus
from patchfrog.review.domain import ReviewCandidate, ReviewCandidateReason
from patchfrog.test_intelligence.domain import (
    TEST_INTELLIGENCE_VERSION,
    PotentialTestGap,
    TestEvidence,
    TestExpectation,
    TestExpectationReasonCode,
    TestIntelligenceReport,
)
from patchfrog.test_intelligence.evidence import evidence_text_for_candidate


def _candidate(*, file_path: str, qualified_name: str | None) -> ReviewCandidate:
    return ReviewCandidate(
        file_path=file_path, symbol_id=uuid.uuid4() if qualified_name else None,
        symbol_name=qualified_name.rsplit(".", 1)[-1] if qualified_name else None,
        qualified_name=qualified_name, start_line=1, end_line=5, changed_lines=(1,),
        static_finding_ids=(), reason=ReviewCandidateReason.CHANGED_SYMBOL,
    )


def _report_with_gap(*, source_file_path: str, source_qualified_name: str) -> TestIntelligenceReport:
    expectation = TestExpectation(
        change_unit_id="u1", source_qualified_name=source_qualified_name, source_file_path=source_file_path,
        reason_code=TestExpectationReasonCode.NO_TEST_SURFACE_FOUND, reason="a real reason",
        evidence=TestEvidence(
            reason_code=TestExpectationReasonCode.NO_TEST_SURFACE_FOUND, bounded_text="no likely test file found"
        ),
        status=CompanionStatus.MISSING,
    )
    gap = PotentialTestGap(change_unit_id="u1", expectation=expectation)
    return TestIntelligenceReport(version=TEST_INTELLIGENCE_VERSION, expectations=(expectation,), gaps=(gap,), test_story="")


def test_empty_for_candidate_with_no_gap() -> None:
    report = _report_with_gap(source_file_path="service.py", source_qualified_name="process_payment")
    unrelated = _candidate(file_path="other.py", qualified_name="other_fn")
    assert evidence_text_for_candidate(report, unrelated) == ""


def test_populated_for_the_exact_gap_candidate() -> None:
    report = _report_with_gap(source_file_path="service.py", source_qualified_name="process_payment")
    candidate = _candidate(file_path="service.py", qualified_name="process_payment")
    text = evidence_text_for_candidate(report, candidate)
    assert "no_test_surface_found" in text
    assert "no likely test file found" in text


def test_empty_for_different_symbol_in_same_file() -> None:
    report = _report_with_gap(source_file_path="service.py", source_qualified_name="process_payment")
    candidate = _candidate(file_path="service.py", qualified_name="unrelated_fn")
    assert evidence_text_for_candidate(report, candidate) == ""


def test_empty_report_never_crashes() -> None:
    report = TestIntelligenceReport(version=TEST_INTELLIGENCE_VERSION, expectations=(), gaps=(), test_story="")
    candidate = _candidate(file_path="service.py", qualified_name="process_payment")
    assert evidence_text_for_candidate(report, candidate) == ""
