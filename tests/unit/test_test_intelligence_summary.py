"""Unit tests for :mod:`patchfrog.test_intelligence.story`/
:mod:`patchfrog.test_intelligence.summary`/
:mod:`patchfrog.test_intelligence.telemetry` -- bounded, deterministic
rendering, never a score/percentage."""

from __future__ import annotations

from patchfrog.change_intelligence.domain import CompanionStatus
from patchfrog.test_intelligence.domain import (
    TEST_INTELLIGENCE_VERSION,
    PotentialTestGap,
    TestEvidence,
    TestExpectation,
    TestExpectationReasonCode,
    TestIntelligenceReport,
)
from patchfrog.test_intelligence.story import build_test_story_prefix
from patchfrog.test_intelligence.summary import (
    render_test_gap_summary,
    should_render_test_gap_summary,
)
from patchfrog.test_intelligence.telemetry import summarize_for_persistence


def _expectation(reason_code: TestExpectationReasonCode, *, file_path: str = "service.py") -> TestExpectation:
    return TestExpectation(
        change_unit_id="u1", source_qualified_name="process_payment", source_file_path=file_path,
        reason_code=reason_code, reason="a real reason", evidence=TestEvidence(reason_code=reason_code, bounded_text="evidence"),
        status=CompanionStatus.MISSING,
    )


def _gap(reason_code: TestExpectationReasonCode) -> PotentialTestGap:
    return PotentialTestGap(change_unit_id="u1", expectation=_expectation(reason_code))


def test_story_prefix_empty_with_no_gaps() -> None:
    assert build_test_story_prefix(()) == ""


def test_story_prefix_mentions_both_reason_kinds() -> None:
    gaps = (
        _gap(TestExpectationReasonCode.NO_TEST_SURFACE_FOUND),
        _gap(TestExpectationReasonCode.TEST_TOUCHED_BUT_WEAKENED),
    )
    prefix = build_test_story_prefix(gaps)
    assert "no discoverable test surface" in prefix
    assert "weakened structural test signal" in prefix


def test_summary_not_rendered_with_no_gaps() -> None:
    report = TestIntelligenceReport(version=TEST_INTELLIGENCE_VERSION, expectations=(), gaps=(), test_story="")
    assert not should_render_test_gap_summary(report)
    assert render_test_gap_summary(report) is None


def test_summary_rendered_with_a_real_gap() -> None:
    gaps = (_gap(TestExpectationReasonCode.NO_TEST_SURFACE_FOUND),)
    report = TestIntelligenceReport(version=TEST_INTELLIGENCE_VERSION, expectations=(), gaps=gaps, test_story="")
    assert should_render_test_gap_summary(report)
    text = render_test_gap_summary(report)
    assert text is not None
    assert "### Test coverage" in text
    assert "process_payment" in text


def test_persistence_summary_counts_and_renders() -> None:
    expectations = (
        _expectation(TestExpectationReasonCode.NO_TEST_SURFACE_FOUND),
        _expectation(TestExpectationReasonCode.TEST_TOUCHED_BUT_WEAKENED, file_path="test_service.py"),
    )
    gaps = tuple(PotentialTestGap(change_unit_id="u1", expectation=e) for e in expectations)
    report = TestIntelligenceReport(version=TEST_INTELLIGENCE_VERSION, expectations=expectations, gaps=gaps, test_story="")
    summary = summarize_for_persistence(report)
    assert summary.test_expectation_count == 2
    assert summary.test_gap_candidate_count == 2
    assert summary.test_coverage_summary_rendered is True
    assert summary.test_coverage_summary_text is not None
    assert '"no_test_surface_found":1' in summary.test_reason_code_counts_json
    assert '"test_touched_but_weakened":1' in summary.test_reason_code_counts_json


def test_persistence_summary_empty_report() -> None:
    report = TestIntelligenceReport(version=TEST_INTELLIGENCE_VERSION, expectations=(), gaps=(), test_story="")
    summary = summarize_for_persistence(report)
    assert summary.test_expectation_count == 0
    assert summary.test_gap_candidate_count == 0
    assert summary.test_coverage_summary_rendered is False
    assert summary.test_coverage_summary_text is None
    assert summary.test_reason_code_counts_json == "{}"
