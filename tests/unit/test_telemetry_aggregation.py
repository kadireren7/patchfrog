"""Pure-logic tests for :mod:`patchfrog.telemetry.aggregation` -- no
database. Builds :class:`~patchfrog.telemetry.domain.FindingLifecycleTelemetry`/
:class:`~patchfrog.telemetry.domain.FeedbackTelemetry`/
:class:`~patchfrog.telemetry.domain.ReviewTelemetrySnapshot` fixtures by
hand rather than going through the collector."""

from __future__ import annotations

import uuid

from patchfrog.analysis.domain import Confidence, FindingCategory, Severity
from patchfrog.feedback.domain import (
    FeedbackEventType,
    FeedbackSource,
    ResolutionState,
    SignalPolarity,
    SignalStrength,
)
from patchfrog.review.agents.roles import AgentRole
from patchfrog.review.domain import CriticDecision, ProposalStatus, ValidationOutcome
from patchfrog.review.effort_types import ReviewEffortTier
from patchfrog.telemetry.aggregation import (
    aggregate_snapshots,
    compute_critic_decisions_by_role,
    compute_feedback_coverage,
    compute_lifecycle_outcome_counts,
    compute_quality_funnel,
    compute_review_feedback_summary,
    compute_role_funnel,
    compute_tier_funnel,
    compute_validation_outcomes_by_role,
)
from patchfrog.telemetry.domain import (
    TELEMETRY_SCHEMA_VERSION,
    FeedbackScope,
    FeedbackTelemetry,
    FindingLifecycleOutcome,
    FindingLifecycleTelemetry,
    ProviderTelemetry,
    ReviewFeedbackEventTelemetry,
    ReviewTelemetrySnapshot,
)


def _entry(
    *,
    role: AgentRole | None = AgentRole.CORRECTNESS,
    tier: ReviewEffortTier | None = ReviewEffortTier.STANDARD,
    status: ProposalStatus = ProposalStatus.ACCEPTED,
    critic_decision: CriticDecision | None = None,
    validation_outcome: ValidationOutcome | None = ValidationOutcome.VALID,
    outcome: FindingLifecycleOutcome = FindingLifecycleOutcome.ACCEPTED_FINAL,
    finding_id: uuid.UUID | None = None,
    candidate_id: uuid.UUID | None = None,
) -> FindingLifecycleTelemetry:
    return FindingLifecycleTelemetry(
        proposal_id=uuid.uuid4(),
        candidate_id=candidate_id or uuid.uuid4(),
        finding_id=finding_id,
        agent_role=role,
        category=FindingCategory.CORRECTNESS,
        severity=Severity.MEDIUM,
        confidence=Confidence.HIGH,
        file_path="a.py",
        start_line=1,
        end_line=1,
        validation_outcome=validation_outcome,
        status=status,
        critic_decision=critic_decision,
        outcome=outcome,
        effort_tier=tier,
    )


def test_lifecycle_outcome_counts_never_merges_suppression_reasons() -> None:
    entries = [
        _entry(status=ProposalStatus.SUPPRESSED_DUPLICATE, outcome=FindingLifecycleOutcome.SUPPRESSED_DUPLICATE),
        _entry(
            status=ProposalStatus.SUPPRESSED_CONTRADICTION,
            outcome=FindingLifecycleOutcome.SUPPRESSED_CONTRADICTION,
        ),
        _entry(status=ProposalStatus.SUPPRESSED_BUDGET, outcome=FindingLifecycleOutcome.SUPPRESSED_BUDGET),
    ]
    counts = compute_lifecycle_outcome_counts(entries)
    assert counts == {
        "suppressed_duplicate": 1,
        "suppressed_contradiction": 1,
        "suppressed_budget": 1,
    }


def test_validation_outcomes_by_role_breaks_down_correctly() -> None:
    entries = [
        _entry(role=AgentRole.SECURITY, validation_outcome=ValidationOutcome.HALLUCINATED_EVIDENCE),
        _entry(role=AgentRole.SECURITY, validation_outcome=ValidationOutcome.HALLUCINATED_EVIDENCE),
        _entry(role=AgentRole.CORRECTNESS, validation_outcome=ValidationOutcome.VALID),
    ]
    breakdown = compute_validation_outcomes_by_role(entries)
    assert breakdown["security"] == {"hallucinated_evidence": 2}
    assert breakdown["correctness"] == {"valid": 1}


def test_critic_decisions_by_role() -> None:
    entries = [
        _entry(role=AgentRole.CORRECTNESS, critic_decision=CriticDecision.ACCEPT),
        _entry(role=AgentRole.CORRECTNESS, critic_decision=CriticDecision.REJECT),
        _entry(role=AgentRole.SECURITY, critic_decision=None),  # never critiqued -- excluded, not zero-filled
    ]
    breakdown = compute_critic_decisions_by_role(entries)
    assert breakdown["correctness"] == {"accept": 1, "reject": 1}
    assert "security" not in breakdown


def test_role_funnel_reports_minimum_required_stages() -> None:
    entries = [
        _entry(role=AgentRole.CORRECTNESS, outcome=FindingLifecycleOutcome.ACCEPTED_FINAL),
        _entry(role=AgentRole.CORRECTNESS, outcome=FindingLifecycleOutcome.VALIDATION_REJECTED),
        _entry(role=AgentRole.SECURITY, outcome=FindingLifecycleOutcome.CRITIC_REJECTED),
    ]
    funnel = compute_role_funnel(entries)
    by_key = {f.key: f for f in funnel}
    assert by_key["correctness"].proposed == 2
    assert by_key["correctness"].accepted_final == 1
    assert by_key["correctness"].validation_rejected == 1
    assert by_key["security"].critic_rejected == 1


def test_tier_funnel_reports_minimum_required_stages() -> None:
    entries = [
        _entry(tier=ReviewEffortTier.LIGHT, outcome=FindingLifecycleOutcome.ACCEPTED_FINAL),
        _entry(tier=ReviewEffortTier.DEEP, outcome=FindingLifecycleOutcome.CRITIC_REJECTED),
    ]
    funnel = compute_tier_funnel(entries)
    by_key = {f.key: f for f in funnel}
    assert by_key["light"].accepted_final == 1
    assert by_key["deep"].critic_rejected == 1


def test_quality_funnel_counts_every_proposal_exactly_once() -> None:
    entries = [
        _entry(status=ProposalStatus.ACCEPTED, outcome=FindingLifecycleOutcome.ACCEPTED_FINAL, finding_id=uuid.uuid4()),
        _entry(
            status=ProposalStatus.ACCEPTED,
            critic_decision=CriticDecision.DOWNGRADE,
            outcome=FindingLifecycleOutcome.CRITIC_DOWNGRADED,
            finding_id=uuid.uuid4(),
        ),
        _entry(status=ProposalStatus.REJECTED_VALIDATION, outcome=FindingLifecycleOutcome.VALIDATION_REJECTED),
        _entry(status=ProposalStatus.REJECTED_CRITIC, outcome=FindingLifecycleOutcome.CRITIC_REJECTED),
        _entry(status=ProposalStatus.SUPPRESSED_DUPLICATE, outcome=FindingLifecycleOutcome.SUPPRESSED_DUPLICATE),
    ]
    funnel = compute_quality_funnel(candidate_count=3, entries=entries, feedback=())
    assert funnel.proposals == 5
    assert funnel.accepted_final == 2
    # drop_off + accepted_final accounts for every proposal exactly once,
    # with no double counting (spec section 29).
    assert sum(funnel.drop_off.values()) + funnel.accepted_final == funnel.proposals
    assert funnel.published_findings == 2


def test_feedback_coverage_denominators_are_feedback_bearing_only() -> None:
    feedback = (
        FeedbackTelemetry(
            finding_id=uuid.uuid4(), has_feedback=True, usefulness_signal=SignalPolarity.POSITIVE,
            resolution_signal=ResolutionState.OPEN, explicit_useful=1, explicit_false_positive=0,
            explicit_fixed=0, explicit_ignore=0, positive_reactions=1, negative_reactions=0,
        ),
        FeedbackTelemetry(
            finding_id=uuid.uuid4(), has_feedback=True, usefulness_signal=SignalPolarity.NEGATIVE,
            resolution_signal=ResolutionState.OPEN, explicit_useful=0, explicit_false_positive=1,
            explicit_fixed=0, explicit_ignore=0, positive_reactions=0, negative_reactions=1,
        ),
        FeedbackTelemetry(
            finding_id=uuid.uuid4(), has_feedback=False, usefulness_signal=None,
            resolution_signal=None, explicit_useful=0, explicit_false_positive=0,
            explicit_fixed=0, explicit_ignore=0, positive_reactions=0, negative_reactions=0,
        ),
    )
    coverage = compute_feedback_coverage(feedback)
    assert coverage.published_findings == 3
    assert coverage.feedback_bearing_findings == 2
    assert coverage.coverage_rate == 2 / 3
    # useful_rate / false_positive_rate denominated by feedback-bearing
    # (2), never by all published findings (3) -- missing feedback is
    # never counted as either positive or negative evidence.
    assert coverage.useful_rate == 1 / 2
    assert coverage.user_reported_false_positive_rate == 1 / 2


def test_feedback_coverage_empty_is_zero_not_error() -> None:
    coverage = compute_feedback_coverage(())
    assert coverage.coverage_rate == 0.0
    assert coverage.useful_rate == 0.0


def test_aggregate_snapshots_sums_across_runs() -> None:
    def _snapshot(run_id: uuid.UUID, *, calls: int) -> ReviewTelemetrySnapshot:
        return ReviewTelemetrySnapshot(
            schema_version=TELEMETRY_SCHEMA_VERSION, review_run_id=run_id, repository_id=uuid.uuid4(),
            pull_request_id=None, status="succeeded", commit_sha="a" * 40, started_at="2026-01-01T00:00:00+00:00",
            completed_at=None, duration_ms=100.0, candidate_count=2, candidates_reviewed=2, candidates_failed=0,
            candidates_skipped_budget=0, candidates_escalated=0, candidates=(), finding_lifecycle=(),
            provider=ProviderTelemetry(
                reviewer_provider="fake", reviewer_model="fake-1", critic_provider=None, critic_model=None,
                reviewer_calls_total=calls, reviewer_input_tokens_total=10, reviewer_output_tokens_total=5,
                reviewer_thinking_tokens_total=0, reviewer_by_role=(), reviewer_latency_ms_aggregate=50.0,
                critic_calls_total=0, critic_input_tokens_total=0, critic_output_tokens_total=0,
                critic_thinking_tokens_total=0, critic_latency_ms_aggregate=0.0, retries_consumed=0,
            ),
            context=(), feedback=(),
        )

    aggregate = aggregate_snapshots([_snapshot(uuid.uuid4(), calls=3), _snapshot(uuid.uuid4(), calls=5)])
    assert aggregate.review_run_count == 2
    assert aggregate.reviewer_calls_total == 8
    assert aggregate.candidate_count == 4
    assert aggregate.reviewer_latency_ms_aggregate == 100.0


def _review_event(*, event_type: FeedbackEventType, signal: str) -> ReviewFeedbackEventTelemetry:
    return ReviewFeedbackEventTelemetry(
        scope=FeedbackScope.REVIEW, event_type=event_type, source=FeedbackSource.REPLY_SYNC,
        normalized_signal=signal, signal_strength=SignalStrength.STRONG, occurred_at="2026-01-01T00:00:00+00:00",
    )


def test_compute_review_feedback_summary_retains_conflicting_events() -> None:
    events = (
        _review_event(event_type=FeedbackEventType.EXPLICIT_COMMAND, signal="useful"),
        _review_event(event_type=FeedbackEventType.EXPLICIT_COMMAND, signal="false-positive"),
        _review_event(event_type=FeedbackEventType.PR_MERGED, signal="merged"),
    )
    summary = compute_review_feedback_summary(events)
    assert summary.review_feedback_event_count == 3
    # Never collapsed into one fabricated label -- both conflicting
    # "useful" and "false-positive" signals survive as separate counts.
    assert summary.review_feedback_by_signal == {"useful": 1, "false-positive": 1, "merged": 1}
    assert summary.review_feedback_by_event_type == {"explicit_command": 2, "pr_merged": 1}


def test_compute_review_feedback_summary_empty_is_zero_not_error() -> None:
    summary = compute_review_feedback_summary(())
    assert summary.review_feedback_event_count == 0
    assert summary.review_feedback_by_signal == {}
