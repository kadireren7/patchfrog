"""Unit tests for :mod:`patchfrog.repository_learnings.matching` --
deterministic, pure derivation. Every case is a hand-built
:class:`~patchfrog.historical_regression_memory.domain.HistoricalRegressionRecord`/
:class:`~patchfrog.change_intelligence.domain.ChangeUnit`, no database,
no LLM -- mirrors
tests/unit/test_historical_regression_memory_matching.py's own
discipline exactly.
"""

from __future__ import annotations

import uuid

from patchfrog.analysis.domain import FindingCategory
from patchfrog.change_intelligence.domain import ChangeKind, ChangeUnit
from patchfrog.historical_regression_memory.domain import (
    HistoricalEvidenceStrength,
    HistoricalMatchKind,
    HistoricalRegressionReasonCode,
    HistoricalRegressionRecord,
    PotentialHistoricalRegression,
)
from patchfrog.repository_learnings.domain import (
    MIN_SUPPORTING_EVENTS,
    RepositoryLearningApplicationStatus,
    RepositoryLearningPatternKind,
    RepositoryLearningStatus,
)
from patchfrog.repository_learnings.matching import (
    derive_repository_learning_applications,
    derive_repository_learnings,
)
from patchfrog.review.domain import ReviewCandidate, ReviewCandidateReason

_REPO_ID = uuid.uuid4()


def _record(
    *,
    file_path: str = "service.py",
    qualified_name: str | None = "process_payment",
    review_run_id: uuid.UUID | None = None,
    observed_at: str = "2026-01-01T00:00:00+00:00",
    strength: HistoricalEvidenceStrength = HistoricalEvidenceStrength.CONFIRMED_FIXED,
    category: FindingCategory = FindingCategory.CORRECTNESS,
) -> HistoricalRegressionRecord:
    return HistoricalRegressionRecord(
        historical_finding_id=uuid.uuid4(), repository_id=_REPO_ID,
        historical_review_run_id=review_run_id or uuid.uuid4(),
        historical_commit_sha="a" * 40, source_file_path=file_path, source_qualified_name=qualified_name,
        finding_category=category, evidence_strength=strength,
        bounded_evidence_fingerprint="forgot idempotency key", observed_at=observed_at,
    )


def _candidate(*, file_path: str, qualified_name: str | None) -> ReviewCandidate:
    return ReviewCandidate(
        file_path=file_path, symbol_id=uuid.uuid4() if qualified_name else None,
        symbol_name=qualified_name.rsplit(".", 1)[-1] if qualified_name else None,
        qualified_name=qualified_name, start_line=1, end_line=5, changed_lines=(1,),
        static_finding_ids=(), reason=ReviewCandidateReason.CHANGED_SYMBOL,
    )


def _unit(*, file_path: str, qualified_name: str | None, kind: ChangeKind = ChangeKind.BEHAVIOR) -> ChangeUnit:
    return ChangeUnit(
        id="u1", title="payment", change_kind=kind,
        changed_candidates=(_candidate(file_path=file_path, qualified_name=qualified_name),),
    )


def test_single_trusted_event_never_produces_a_learning() -> None:
    """The core distinguishing test vs Milestone N: 1 historical event
    -> N may fire, O must not."""

    records = (_record(),)
    learnings = derive_repository_learnings(trusted_records=records, repository_id=_REPO_ID)
    assert learnings == ()


def test_two_independent_review_runs_activates_a_learning() -> None:
    records = (
        _record(review_run_id=uuid.uuid4(), observed_at="2026-01-01T00:00:00+00:00"),
        _record(review_run_id=uuid.uuid4(), observed_at="2026-02-01T00:00:00+00:00"),
    )
    learnings = derive_repository_learnings(trusted_records=records, repository_id=_REPO_ID)
    assert len(learnings) == 1
    learning = learnings[0]
    assert learning.status is RepositoryLearningStatus.ACTIVE
    assert learning.support_count == MIN_SUPPORTING_EVENTS == 2
    assert learning.pattern.pattern_kind is RepositoryLearningPatternKind.REPEATED_SAME_SURFACE_REGRESSION
    assert learning.activated_at == "2026-02-01T00:00:00+00:00"
    assert learning.first_observed_at == "2026-01-01T00:00:00+00:00"
    assert learning.last_observed_at == "2026-02-01T00:00:00+00:00"


def test_two_findings_from_the_same_review_run_do_not_satisfy_independence() -> None:
    """Two findings from one review run were never independently
    re-observed over time -- must not satisfy the gate."""

    run_id = uuid.uuid4()
    records = (
        _record(review_run_id=run_id, observed_at="2026-01-01T00:00:00+00:00"),
        _record(review_run_id=run_id, observed_at="2026-01-01T01:00:00+00:00"),
    )
    learnings = derive_repository_learnings(trusted_records=records, repository_id=_REPO_ID)
    assert learnings == ()


def test_activation_time_is_the_minimum_support_set_not_the_most_recent_event() -> None:
    """A third, later event must not move activated_at forward -- it is
    fixed at the moment the *minimum* support set (the earliest
    MIN_SUPPORTING_EVENTS runs) first satisfied the gate."""

    records = (
        _record(review_run_id=uuid.uuid4(), observed_at="2026-01-01T00:00:00+00:00"),
        _record(review_run_id=uuid.uuid4(), observed_at="2026-02-01T00:00:00+00:00"),
        _record(review_run_id=uuid.uuid4(), observed_at="2026-06-01T00:00:00+00:00"),
    )
    learnings = derive_repository_learnings(trusted_records=records, repository_id=_REPO_ID)
    assert len(learnings) == 1
    learning = learnings[0]
    assert learning.support_count == 3
    assert learning.activated_at == "2026-02-01T00:00:00+00:00"
    assert learning.last_observed_at == "2026-06-01T00:00:00+00:00"


def test_record_with_no_qualified_name_never_participates() -> None:
    records = (
        _record(qualified_name=None, review_run_id=uuid.uuid4(), observed_at="2026-01-01T00:00:00+00:00"),
        _record(qualified_name=None, review_run_id=uuid.uuid4(), observed_at="2026-02-01T00:00:00+00:00"),
    )
    learnings = derive_repository_learnings(trusted_records=records, repository_id=_REPO_ID)
    assert learnings == ()


def test_different_surfaces_never_pooled_together() -> None:
    records = (
        _record(file_path="a.py", qualified_name="foo", review_run_id=uuid.uuid4()),
        _record(file_path="b.py", qualified_name="bar", review_run_id=uuid.uuid4()),
    )
    learnings = derive_repository_learnings(trusted_records=records, repository_id=_REPO_ID)
    assert learnings == ()


def test_application_requires_anchor_directly_changed_in_current_pr() -> None:
    records = (
        _record(review_run_id=uuid.uuid4(), observed_at="2026-01-01T00:00:00+00:00"),
        _record(review_run_id=uuid.uuid4(), observed_at="2026-02-01T00:00:00+00:00"),
    )
    learnings = derive_repository_learnings(trusted_records=records, repository_id=_REPO_ID)

    unrelated_unit = _unit(file_path="other.py", qualified_name="unrelated_fn")
    applications = derive_repository_learning_applications(learnings=learnings, change_units=(unrelated_unit,))
    assert applications == ()

    matching_unit = _unit(file_path="service.py", qualified_name="process_payment")
    applications = derive_repository_learning_applications(learnings=learnings, change_units=(matching_unit,))
    assert len(applications) == 1
    application = applications[0]
    assert application.status is RepositoryLearningApplicationStatus.UNSATISFIED
    assert application.stands_alone
    assert application.current_change_unit_id == "u1"


def test_test_only_change_unit_never_triggers_an_application() -> None:
    """Mirrors N's own TEST-kind ChangeUnit exclusion exactly -- a
    test-only PR that merely calls a learned-risky symbol must never
    trigger an application."""

    records = (
        _record(review_run_id=uuid.uuid4(), observed_at="2026-01-01T00:00:00+00:00"),
        _record(review_run_id=uuid.uuid4(), observed_at="2026-02-01T00:00:00+00:00"),
    )
    learnings = derive_repository_learnings(trusted_records=records, repository_id=_REPO_ID)

    test_unit = _unit(file_path="service.py", qualified_name="process_payment", kind=ChangeKind.TEST)
    applications = derive_repository_learning_applications(learnings=learnings, change_units=(test_unit,))
    assert applications == ()


def test_application_enriches_existing_n_candidate_on_same_surface() -> None:
    records = (
        _record(review_run_id=uuid.uuid4(), observed_at="2026-01-01T00:00:00+00:00"),
        _record(review_run_id=uuid.uuid4(), observed_at="2026-02-01T00:00:00+00:00"),
    )
    learnings = derive_repository_learnings(trusted_records=records, repository_id=_REPO_ID)
    unit = _unit(file_path="service.py", qualified_name="process_payment")

    n_candidate = PotentialHistoricalRegression(
        current_change_unit_id="u1", current_file_path="service.py", current_qualified_name="process_payment",
        historical_record=records[-1], match_kind=HistoricalMatchKind.SAME_SYMBOL,
        reason_code=HistoricalRegressionReasonCode.PREVIOUS_FIXED_FINDING_SAME_SYMBOL,
        evidence="a previous fixed finding involved 'process_payment'",
    )

    applications = derive_repository_learning_applications(
        learnings=learnings, change_units=(unit,), historical_candidates=(n_candidate,)
    )
    assert len(applications) == 1
    application = applications[0]
    assert not application.stands_alone
    assert application.enriches_historical_regression is n_candidate


def test_application_stands_alone_when_no_n_candidate_passed() -> None:
    records = (
        _record(review_run_id=uuid.uuid4(), observed_at="2026-01-01T00:00:00+00:00"),
        _record(review_run_id=uuid.uuid4(), observed_at="2026-02-01T00:00:00+00:00"),
    )
    learnings = derive_repository_learnings(trusted_records=records, repository_id=_REPO_ID)
    unit = _unit(file_path="service.py", qualified_name="process_payment")

    applications = derive_repository_learning_applications(learnings=learnings, change_units=(unit,))
    assert len(applications) == 1
    assert applications[0].stands_alone


def test_earliest_finding_category_names_the_pattern() -> None:
    records = (
        _record(
            review_run_id=uuid.uuid4(), observed_at="2026-01-01T00:00:00+00:00",
            category=FindingCategory.SECURITY,
        ),
        _record(
            review_run_id=uuid.uuid4(), observed_at="2026-02-01T00:00:00+00:00",
            category=FindingCategory.CORRECTNESS,
        ),
    )
    learnings = derive_repository_learnings(trusted_records=records, repository_id=_REPO_ID)
    assert learnings[0].pattern.finding_category is FindingCategory.SECURITY


def test_learning_id_is_deterministic_for_same_pattern_identity() -> None:
    records_a = (
        _record(review_run_id=uuid.uuid4(), observed_at="2026-01-01T00:00:00+00:00"),
        _record(review_run_id=uuid.uuid4(), observed_at="2026-02-01T00:00:00+00:00"),
    )
    records_b = (
        _record(review_run_id=uuid.uuid4(), observed_at="2026-03-01T00:00:00+00:00"),
        _record(review_run_id=uuid.uuid4(), observed_at="2026-04-01T00:00:00+00:00"),
    )
    learning_a = derive_repository_learnings(trusted_records=records_a, repository_id=_REPO_ID)[0]
    learning_b = derive_repository_learnings(trusted_records=records_b, repository_id=_REPO_ID)[0]
    assert learning_a.learning_id == learning_b.learning_id
