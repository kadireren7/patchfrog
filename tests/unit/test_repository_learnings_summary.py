"""Unit tests for :mod:`patchfrog.repository_learnings.story`/
:mod:`patchfrog.repository_learnings.summary`/
:mod:`patchfrog.repository_learnings.telemetry` -- bounded,
deterministic rendering, never a score/percentage/count-of-past-bugs."""

from __future__ import annotations

import uuid

from patchfrog.analysis.domain import FindingCategory
from patchfrog.historical_regression_memory.domain import (
    HistoricalEvidenceStrength,
    HistoricalRegressionRecord,
)
from patchfrog.repository_learnings.domain import (
    REPOSITORY_LEARNINGS_VERSION,
    PotentialRepositoryLearningApplication,
    RepositoryLearning,
    RepositoryLearningApplicationStatus,
    RepositoryLearningEvidence,
    RepositoryLearningPattern,
    RepositoryLearningPatternKind,
    RepositoryLearningsReport,
    RepositoryLearningStatus,
)
from patchfrog.repository_learnings.story import build_repository_learning_story_prefix
from patchfrog.repository_learnings.summary import (
    render_repository_learning_summary,
    should_render_repository_learning_summary,
)
from patchfrog.repository_learnings.telemetry import summarize_for_persistence

_REPO_ID = uuid.uuid4()


def _record(observed_at: str) -> HistoricalRegressionRecord:
    return HistoricalRegressionRecord(
        historical_finding_id=uuid.uuid4(), repository_id=_REPO_ID, historical_review_run_id=uuid.uuid4(),
        historical_commit_sha="a" * 40, source_file_path="retry_worker.py", source_qualified_name="RetryWorker.run",
        finding_category=FindingCategory.CORRECTNESS, evidence_strength=HistoricalEvidenceStrength.CONFIRMED_FIXED,
        bounded_evidence_fingerprint="forgot idempotency key", observed_at=observed_at,
    )


def _learning() -> RepositoryLearning:
    r1, r2 = _record("2026-01-01T00:00:00+00:00"), _record("2026-02-01T00:00:00+00:00")
    pattern = RepositoryLearningPattern(
        repository_id=_REPO_ID, pattern_kind=RepositoryLearningPatternKind.REPEATED_SAME_SURFACE_REGRESSION,
        anchor_file_path="retry_worker.py", anchor_qualified_name="RetryWorker.run",
        finding_category=FindingCategory.CORRECTNESS,
    )
    return RepositoryLearning(
        learning_id="fixedid", pattern=pattern, status=RepositoryLearningStatus.ACTIVE,
        supporting_evidence=(RepositoryLearningEvidence(historical_record=r1), RepositoryLearningEvidence(historical_record=r2)),
        support_count=2, activated_at="2026-02-01T00:00:00+00:00",
        first_observed_at="2026-01-01T00:00:00+00:00", last_observed_at="2026-02-01T00:00:00+00:00",
    )


def _application() -> PotentialRepositoryLearningApplication:
    return PotentialRepositoryLearningApplication(
        learning=_learning(), current_change_unit_id="u1", current_file_path="retry_worker.py",
        current_qualified_name="RetryWorker.run", status=RepositoryLearningApplicationStatus.UNSATISFIED,
        evidence="this exact surface has produced 2 independently trusted findings",
    )


def test_story_prefix_empty_with_no_applications() -> None:
    assert build_repository_learning_story_prefix(()) == ""


def test_story_prefix_rendered_for_real_application() -> None:
    prefix = build_repository_learning_story_prefix((_application(),))
    assert "Repository learning" in prefix
    assert "RetryWorker.run" in prefix
    assert "2 independent reviews" in prefix


def test_summary_not_rendered_with_no_applications() -> None:
    report = RepositoryLearningsReport(
        version=REPOSITORY_LEARNINGS_VERSION, learnings_considered=(_learning(),), applications=(),
        repository_learning_story="",
    )
    assert not should_render_repository_learning_summary(report)
    assert render_repository_learning_summary(report) is None


def test_summary_rendered_for_real_application() -> None:
    report = RepositoryLearningsReport(
        version=REPOSITORY_LEARNINGS_VERSION, learnings_considered=(_learning(),), applications=(_application(),),
        repository_learning_story="",
    )
    assert should_render_repository_learning_summary(report)
    text = render_repository_learning_summary(report)
    assert text is not None
    assert "### Repository learning" in text
    assert "RetryWorker.run" in text
    # Never a score/percentage/count-of-past-bugs beyond the plain support count.
    assert "%" not in text


def test_persistence_summary_counts_and_renders() -> None:
    report = RepositoryLearningsReport(
        version=REPOSITORY_LEARNINGS_VERSION, learnings_considered=(_learning(),), applications=(_application(),),
        repository_learning_story="",
    )
    summary = summarize_for_persistence(report)
    assert summary.repository_learning_active_count == 1
    assert summary.repository_learning_application_count == 1
    assert summary.repository_learning_summary_rendered is True
    assert summary.repository_learning_summary_text is not None


def test_persistence_summary_empty_report() -> None:
    report = RepositoryLearningsReport(
        version=REPOSITORY_LEARNINGS_VERSION, learnings_considered=(), applications=(),
        repository_learning_story="",
    )
    summary = summarize_for_persistence(report)
    assert summary.repository_learning_active_count == 0
    assert summary.repository_learning_application_count == 0
    assert summary.repository_learning_summary_rendered is False
    assert summary.repository_learning_summary_text is None
