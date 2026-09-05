"""Unit tests for :mod:`patchfrog.repository_learnings.evidence` --
bounded, per-candidate evidence text, empty for every candidate with
no active learning application of its own (Quality + Cost Guard budget
discipline)."""

from __future__ import annotations

import uuid

from patchfrog.analysis.domain import FindingCategory
from patchfrog.historical_regression_memory.domain import (
    HistoricalEvidenceStrength,
    HistoricalMatchKind,
    HistoricalRegressionReasonCode,
    HistoricalRegressionRecord,
    PotentialHistoricalRegression,
)
from patchfrog.repository_learnings.domain import (
    REPOSITORY_LEARNINGS_VERSION,
    PotentialRepositoryLearningApplication,
    RepositoryLearning,
    RepositoryLearningEvidence,
    RepositoryLearningPattern,
    RepositoryLearningPatternKind,
    RepositoryLearningsReport,
    RepositoryLearningStatus,
)
from patchfrog.repository_learnings.evidence import evidence_text_for_candidate
from patchfrog.review.domain import ReviewCandidate, ReviewCandidateReason

_REPO_ID = uuid.uuid4()


def _candidate(*, file_path: str, qualified_name: str | None) -> ReviewCandidate:
    return ReviewCandidate(
        file_path=file_path, symbol_id=uuid.uuid4() if qualified_name else None,
        symbol_name=qualified_name.rsplit(".", 1)[-1] if qualified_name else None,
        qualified_name=qualified_name, start_line=1, end_line=5, changed_lines=(1,),
        static_finding_ids=(), reason=ReviewCandidateReason.CHANGED_SYMBOL,
    )


def _record(observed_at: str, *, file_path: str, qualified_name: str) -> HistoricalRegressionRecord:
    return HistoricalRegressionRecord(
        historical_finding_id=uuid.uuid4(), repository_id=_REPO_ID, historical_review_run_id=uuid.uuid4(),
        historical_commit_sha="a" * 40, source_file_path=file_path, source_qualified_name=qualified_name,
        finding_category=FindingCategory.CORRECTNESS, evidence_strength=HistoricalEvidenceStrength.CONFIRMED_FIXED,
        bounded_evidence_fingerprint="forgot idempotency key", observed_at=observed_at,
    )


def _report_with_application(*, file_path: str, qualified_name: str) -> RepositoryLearningsReport:
    r1 = _record("2026-01-01T00:00:00+00:00", file_path=file_path, qualified_name=qualified_name)
    r2 = _record("2026-02-01T00:00:00+00:00", file_path=file_path, qualified_name=qualified_name)
    pattern = RepositoryLearningPattern(
        repository_id=_REPO_ID, pattern_kind=RepositoryLearningPatternKind.REPEATED_SAME_SURFACE_REGRESSION,
        anchor_file_path=file_path, anchor_qualified_name=qualified_name,
        finding_category=FindingCategory.CORRECTNESS,
    )
    learning = RepositoryLearning(
        learning_id="fixedid", pattern=pattern, status=RepositoryLearningStatus.ACTIVE,
        supporting_evidence=(RepositoryLearningEvidence(historical_record=r1), RepositoryLearningEvidence(historical_record=r2)),
        support_count=2, activated_at="2026-02-01T00:00:00+00:00",
        first_observed_at="2026-01-01T00:00:00+00:00", last_observed_at="2026-02-01T00:00:00+00:00",
    )
    n_candidate = PotentialHistoricalRegression(
        current_change_unit_id="u1", current_file_path=file_path, current_qualified_name=qualified_name,
        historical_record=r2, match_kind=HistoricalMatchKind.SAME_SYMBOL,
        reason_code=HistoricalRegressionReasonCode.PREVIOUS_FIXED_FINDING_SAME_SYMBOL, evidence="matched",
    )
    application = PotentialRepositoryLearningApplication(
        learning=learning, current_change_unit_id="u1", current_file_path=file_path,
        current_qualified_name=qualified_name,
        evidence="this exact surface has produced 2 independently trusted findings",
        enriches_historical_regression=n_candidate,
    )
    return RepositoryLearningsReport(
        version=REPOSITORY_LEARNINGS_VERSION, learnings_considered=(learning,), applications=(application,),
        repository_learning_story="",
    )


def test_empty_for_candidate_with_no_application() -> None:
    report = _report_with_application(file_path="service.py", qualified_name="process_payment")
    unrelated = _candidate(file_path="other.py", qualified_name="other_fn")
    assert evidence_text_for_candidate(report, unrelated) == ""


def test_populated_for_the_exact_match_candidate() -> None:
    report = _report_with_application(file_path="service.py", qualified_name="process_payment")
    candidate = _candidate(file_path="service.py", qualified_name="process_payment")
    text = evidence_text_for_candidate(report, candidate)
    assert "repeated_same_surface_regression" in text
    assert "correctness" in text
    assert "2 independent trusted findings" in text


def test_empty_for_different_symbol_in_same_file() -> None:
    report = _report_with_application(file_path="service.py", qualified_name="process_payment")
    candidate = _candidate(file_path="service.py", qualified_name="unrelated_fn")
    assert evidence_text_for_candidate(report, candidate) == ""


def test_empty_report_never_crashes() -> None:
    report = RepositoryLearningsReport(
        version=REPOSITORY_LEARNINGS_VERSION, learnings_considered=(), applications=(),
        repository_learning_story="",
    )
    candidate = _candidate(file_path="service.py", qualified_name="process_payment")
    assert evidence_text_for_candidate(report, candidate) == ""
