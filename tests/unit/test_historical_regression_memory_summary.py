"""Unit tests for :mod:`patchfrog.historical_regression_memory.story`/
:mod:`patchfrog.historical_regression_memory.summary`/
:mod:`patchfrog.historical_regression_memory.telemetry` -- bounded,
deterministic rendering, never a score/percentage/count-of-past-bugs."""

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
from patchfrog.historical_regression_memory.story import build_historical_story_prefix
from patchfrog.historical_regression_memory.summary import (
    render_historical_summary,
    should_render_historical_summary,
)
from patchfrog.historical_regression_memory.telemetry import summarize_for_persistence


def _record() -> HistoricalRegressionRecord:
    return HistoricalRegressionRecord(
        historical_finding_id=uuid.uuid4(), repository_id=uuid.uuid4(), historical_review_run_id=uuid.uuid4(),
        historical_commit_sha="a" * 40, source_file_path="retry_worker.py", source_qualified_name="RetryWorker.run",
        finding_category=FindingCategory.CORRECTNESS, evidence_strength=HistoricalEvidenceStrength.CONFIRMED_FIXED,
        bounded_evidence_fingerprint="forgot idempotency key", observed_at="2026-01-01T00:00:00+00:00",
    )


def _candidate(match_kind: HistoricalMatchKind) -> PotentialHistoricalRegression:
    reason = {
        HistoricalMatchKind.SAME_SYMBOL: HistoricalRegressionReasonCode.PREVIOUS_FIXED_FINDING_SAME_SYMBOL,
        HistoricalMatchKind.SAME_QUALIFIED_NAME_IN_SAME_FILE: HistoricalRegressionReasonCode.PREVIOUS_FIXED_FINDING_SAME_SYMBOL,
        HistoricalMatchKind.SAME_FILE: HistoricalRegressionReasonCode.PREVIOUS_FIXED_FINDING_SAME_FILE,
        HistoricalMatchKind.GRAPH_RELATED_SURFACE: HistoricalRegressionReasonCode.PREVIOUS_REGRESSION_RELATED_SURFACE,
    }[match_kind]
    return PotentialHistoricalRegression(
        current_change_unit_id="u1", current_file_path="retry_worker.py", current_qualified_name="RetryWorker.run",
        historical_record=_record(), match_kind=match_kind, reason_code=reason, evidence="matched",
    )


def test_story_prefix_empty_with_no_candidates() -> None:
    assert build_historical_story_prefix(()) == ""


def test_story_prefix_rendered_for_same_symbol() -> None:
    prefix = build_historical_story_prefix((_candidate(HistoricalMatchKind.SAME_SYMBOL),))
    assert "Historical context" in prefix
    assert "RetryWorker.run" in prefix


def test_story_prefix_silent_for_weak_matches() -> None:
    assert build_historical_story_prefix((_candidate(HistoricalMatchKind.SAME_FILE),)) == ""
    assert build_historical_story_prefix((_candidate(HistoricalMatchKind.GRAPH_RELATED_SURFACE),)) == ""


def test_summary_not_rendered_for_weak_matches_only() -> None:
    report = HistoricalRegressionReport(
        version=HISTORICAL_REGRESSION_MEMORY_VERSION, trusted_records_considered=(_record(),),
        candidates=(_candidate(HistoricalMatchKind.SAME_FILE),), historical_story="",
    )
    assert not should_render_historical_summary(report)
    assert render_historical_summary(report) is None


def test_summary_rendered_for_strong_match() -> None:
    report = HistoricalRegressionReport(
        version=HISTORICAL_REGRESSION_MEMORY_VERSION, trusted_records_considered=(_record(),),
        candidates=(_candidate(HistoricalMatchKind.SAME_QUALIFIED_NAME_IN_SAME_FILE),), historical_story="",
    )
    assert should_render_historical_summary(report)
    text = render_historical_summary(report)
    assert text is not None
    assert "### Historical context" in text
    assert "RetryWorker.run" in text
    # Never a count/score/percentage.
    assert "%" not in text
    assert "past bugs" not in text.lower()


def test_persistence_summary_counts_and_renders() -> None:
    candidates = (_candidate(HistoricalMatchKind.SAME_SYMBOL), _candidate(HistoricalMatchKind.SAME_FILE))
    report = HistoricalRegressionReport(
        version=HISTORICAL_REGRESSION_MEMORY_VERSION, trusted_records_considered=(_record(), _record()),
        candidates=candidates, historical_story="",
    )
    summary = summarize_for_persistence(report)
    assert summary.historical_trusted_record_count == 2
    assert summary.historical_regression_candidate_count == 2
    assert summary.historical_summary_rendered is True
    assert '"same_file":1' in summary.historical_match_kind_counts_json
    assert '"same_symbol":1' in summary.historical_match_kind_counts_json


def test_persistence_summary_empty_report() -> None:
    report = HistoricalRegressionReport(
        version=HISTORICAL_REGRESSION_MEMORY_VERSION, trusted_records_considered=(), candidates=(),
        historical_story="",
    )
    summary = summarize_for_persistence(report)
    assert summary.historical_trusted_record_count == 0
    assert summary.historical_regression_candidate_count == 0
    assert summary.historical_summary_rendered is False
    assert summary.historical_summary_text is None
    assert summary.historical_match_kind_counts_json == "{}"
