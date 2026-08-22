"""Unit coverage for :mod:`patchfrog.feedback.assessment` -- the
deterministic rule engine. Every test here anchors to a specific Phase 9
core-principle claim: reactions never touch correctness, resolution never
implies correctness, thread state alone is never proof, and no rule here
maps a single weak signal to a confident verdict."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from patchfrog.feedback.assessment import (
    compute_finding_assessment,
    is_false_positive_candidate,
    is_high_value_candidate,
)
from patchfrog.feedback.domain import (
    ActorIdentity,
    ExplicitCommand,
    FeedbackEvent,
    FeedbackEventType,
    FeedbackSource,
    NormalizedReactionHint,
    ResolutionState,
    SignalPolarity,
    SignalStrength,
)

_FINDING_ID = uuid.uuid4()
_NOW = datetime(2026, 1, 1, tzinfo=UTC)


def _event(
    event_type: FeedbackEventType,
    *,
    normalized_signal: str = "",
    signal_strength: SignalStrength = SignalStrength.WEAK,
    metadata: dict[str, str] | None = None,
    occurred_at: datetime = _NOW,
    source: FeedbackSource = FeedbackSource.REACTION_SYNC,
) -> FeedbackEvent:
    return FeedbackEvent(
        repository_id=uuid.uuid4(),
        pull_request_id=uuid.uuid4(),
        review_run_id=None,
        publication_id=None,
        review_publication_comment_id=uuid.uuid4(),
        finding_id=_FINDING_ID,
        github_review_id=None,
        github_comment_id=None,
        event_type=event_type,
        source=source,
        external_event_id="x",
        raw_signal="",
        normalized_signal=normalized_signal,
        signal_strength=signal_strength,
        actor=ActorIdentity(login="dev", is_bot=False),
        occurred_at=occurred_at,
        metadata=metadata or {},
    )


def test_no_events_produces_all_unknown_assessment() -> None:
    summary = compute_finding_assessment(_FINDING_ID, [])
    a = summary.assessment
    assert a.usefulness_signal is SignalPolarity.UNKNOWN
    assert a.correctness_signal is SignalPolarity.UNKNOWN
    assert a.resolution_signal is ResolutionState.UNKNOWN
    assert a.engagement_signal is False
    assert a.confidence is None


def test_thumbs_up_moves_usefulness_but_never_correctness() -> None:
    """Phase 9 spec section 8's canonical example, verified directly."""

    event = _event(
        FeedbackEventType.REACTION_ADDED,
        normalized_signal=NormalizedReactionHint.POSITIVE_HINT.value,
        metadata={"reaction_id": "1"},
    )
    summary = compute_finding_assessment(_FINDING_ID, [event])
    assert summary.assessment.usefulness_signal is SignalPolarity.POSITIVE
    assert summary.assessment.correctness_signal is SignalPolarity.UNKNOWN


def test_thumbs_down_moves_usefulness_but_never_correctness() -> None:
    event = _event(
        FeedbackEventType.REACTION_ADDED,
        normalized_signal=NormalizedReactionHint.NEGATIVE_HINT.value,
        metadata={"reaction_id": "1"},
    )
    summary = compute_finding_assessment(_FINDING_ID, [event])
    assert summary.assessment.usefulness_signal is SignalPolarity.NEGATIVE
    assert summary.assessment.correctness_signal is SignalPolarity.UNKNOWN


def test_removed_reaction_no_longer_counts_as_active() -> None:
    added = _event(
        FeedbackEventType.REACTION_ADDED,
        normalized_signal=NormalizedReactionHint.POSITIVE_HINT.value,
        metadata={"reaction_id": "1"},
        occurred_at=_NOW,
    )
    removed = _event(
        FeedbackEventType.REACTION_REMOVED,
        normalized_signal=NormalizedReactionHint.POSITIVE_HINT.value,
        metadata={"reaction_id": "1"},
        occurred_at=datetime(2026, 1, 2, tzinfo=UTC),
    )
    summary = compute_finding_assessment(_FINDING_ID, [added, removed])
    assert summary.positive_reactions == 0
    assert summary.assessment.usefulness_signal is SignalPolarity.UNKNOWN


def test_equal_positive_and_negative_reactions_are_neutral_not_positive() -> None:
    pos = _event(
        FeedbackEventType.REACTION_ADDED,
        normalized_signal=NormalizedReactionHint.POSITIVE_HINT.value,
        metadata={"reaction_id": "1"},
    )
    neg = _event(
        FeedbackEventType.REACTION_ADDED,
        normalized_signal=NormalizedReactionHint.NEGATIVE_HINT.value,
        metadata={"reaction_id": "2"},
    )
    summary = compute_finding_assessment(_FINDING_ID, [pos, neg])
    assert summary.assessment.usefulness_signal is SignalPolarity.NEUTRAL


def test_explicit_useful_outweighs_reactions_and_sets_strong_confidence() -> None:
    negative_reaction = _event(
        FeedbackEventType.REACTION_ADDED,
        normalized_signal=NormalizedReactionHint.NEGATIVE_HINT.value,
        metadata={"reaction_id": "1"},
    )
    explicit = _event(
        FeedbackEventType.EXPLICIT_COMMAND,
        normalized_signal=ExplicitCommand.USEFUL.value,
        signal_strength=SignalStrength.STRONG,
        source=FeedbackSource.REPLY_SYNC,
    )
    summary = compute_finding_assessment(_FINDING_ID, [negative_reaction, explicit])
    assert summary.assessment.usefulness_signal is SignalPolarity.POSITIVE
    assert summary.assessment.confidence is SignalStrength.STRONG


def test_explicit_false_positive_sets_negative_correctness() -> None:
    explicit = _event(
        FeedbackEventType.EXPLICIT_COMMAND,
        normalized_signal=ExplicitCommand.FALSE_POSITIVE.value,
        signal_strength=SignalStrength.STRONG,
        source=FeedbackSource.REPLY_SYNC,
    )
    summary = compute_finding_assessment(_FINDING_ID, [explicit])
    assert summary.assessment.correctness_signal is SignalPolarity.NEGATIVE
    assert summary.assessment.usefulness_signal is SignalPolarity.NEGATIVE
    assert summary.explicit_false_positive == 1


def test_explicit_fixed_sets_positive_correctness() -> None:
    explicit = _event(
        FeedbackEventType.EXPLICIT_COMMAND,
        normalized_signal=ExplicitCommand.FIXED.value,
        signal_strength=SignalStrength.STRONG,
        source=FeedbackSource.REPLY_SYNC,
    )
    summary = compute_finding_assessment(_FINDING_ID, [explicit])
    assert summary.assessment.correctness_signal is SignalPolarity.POSITIVE
    assert summary.explicit_fixed == 1


def test_thread_resolved_sets_resolution_closed_never_correctness() -> None:
    event = _event(FeedbackEventType.THREAD_RESOLVED, source=FeedbackSource.THREAD_SYNC)
    summary = compute_finding_assessment(_FINDING_ID, [event])
    assert summary.thread_resolved is True
    assert summary.assessment.resolution_signal is ResolutionState.CLOSED
    assert summary.assessment.correctness_signal is SignalPolarity.UNKNOWN


def test_thread_reopened_after_resolved_wins_by_latest_timestamp() -> None:
    resolved = _event(FeedbackEventType.THREAD_RESOLVED, source=FeedbackSource.THREAD_SYNC, occurred_at=_NOW)
    reopened = _event(
        FeedbackEventType.THREAD_REOPENED,
        source=FeedbackSource.THREAD_SYNC,
        occurred_at=datetime(2026, 1, 2, tzinfo=UTC),
    )
    summary = compute_finding_assessment(_FINDING_ID, [resolved, reopened])
    assert summary.thread_resolved is False
    assert summary.assessment.resolution_signal is ResolutionState.OPEN


def test_finding_disappeared_is_a_medium_confidence_positive_correctness_signal() -> None:
    event = _event(
        FeedbackEventType.FINDING_DISAPPEARED,
        signal_strength=SignalStrength.MEDIUM,
        source=FeedbackSource.REVIEW_MEMORY,
    )
    summary = compute_finding_assessment(_FINDING_ID, [event])
    assert summary.finding_disappeared is True
    assert summary.assessment.correctness_signal is SignalPolarity.POSITIVE
    assert summary.assessment.confidence is SignalStrength.MEDIUM
    assert "not confirmed" in " ".join(summary.assessment.reasons)


def test_code_unchanged_alone_does_not_imply_negative_correctness() -> None:
    event = _event(
        FeedbackEventType.FINDING_CODE_UNCHANGED, signal_strength=SignalStrength.MEDIUM, source=FeedbackSource.REVIEW_MEMORY
    )
    summary = compute_finding_assessment(_FINDING_ID, [event])
    assert summary.assessment.correctness_signal is SignalPolarity.UNKNOWN


def test_code_unchanged_plus_negative_reaction_is_negative_correctness() -> None:
    unchanged = _event(
        FeedbackEventType.FINDING_CODE_UNCHANGED, signal_strength=SignalStrength.MEDIUM, source=FeedbackSource.REVIEW_MEMORY
    )
    negative_reaction = _event(
        FeedbackEventType.REACTION_ADDED,
        normalized_signal=NormalizedReactionHint.NEGATIVE_HINT.value,
        metadata={"reaction_id": "1"},
    )
    summary = compute_finding_assessment(_FINDING_ID, [unchanged, negative_reaction])
    assert summary.assessment.correctness_signal is SignalPolarity.NEGATIVE


def test_generic_reply_is_only_engagement_never_correctness_or_usefulness() -> None:
    reply = _event(FeedbackEventType.COMMENT_REPLY, normalized_signal="developer_engaged", source=FeedbackSource.REPLY_SYNC)
    summary = compute_finding_assessment(_FINDING_ID, [reply])
    assert summary.assessment.engagement_signal is True
    assert summary.assessment.correctness_signal is SignalPolarity.UNKNOWN
    assert summary.assessment.usefulness_signal is SignalPolarity.UNKNOWN


def test_false_positive_candidate_requires_strong_evidence_not_a_single_reaction() -> None:
    single_negative = _event(
        FeedbackEventType.REACTION_ADDED,
        normalized_signal=NormalizedReactionHint.NEGATIVE_HINT.value,
        metadata={"reaction_id": "1"},
    )
    summary = compute_finding_assessment(_FINDING_ID, [single_negative])
    # One negative reaction with no code-change signal at all -- not
    # enough on its own under the "unchanged code" rule (no lifecycle
    # event present), and not >= 2 negative reactions either.
    assert not is_false_positive_candidate(summary)


def test_false_positive_candidate_true_for_negative_reaction_plus_confirmed_unchanged_code() -> None:
    negative = _event(
        FeedbackEventType.REACTION_ADDED,
        normalized_signal=NormalizedReactionHint.NEGATIVE_HINT.value,
        metadata={"reaction_id": "1"},
    )
    unchanged = _event(
        FeedbackEventType.FINDING_CODE_UNCHANGED, signal_strength=SignalStrength.MEDIUM, source=FeedbackSource.REVIEW_MEMORY
    )
    summary = compute_finding_assessment(_FINDING_ID, [negative, unchanged])
    assert summary.finding_code_unchanged is True
    assert is_false_positive_candidate(summary)


def test_two_negative_reactions_alone_are_sufficient_without_any_code_signal() -> None:
    neg1 = _event(
        FeedbackEventType.REACTION_ADDED,
        normalized_signal=NormalizedReactionHint.NEGATIVE_HINT.value,
        metadata={"reaction_id": "1"},
    )
    neg2 = _event(
        FeedbackEventType.REACTION_ADDED,
        normalized_signal=NormalizedReactionHint.NEGATIVE_HINT.value,
        metadata={"reaction_id": "2"},
    )
    summary = compute_finding_assessment(_FINDING_ID, [neg1, neg2])
    assert summary.finding_code_unchanged is False
    assert is_false_positive_candidate(summary)


def test_false_positive_candidate_true_for_explicit_command() -> None:
    explicit = _event(
        FeedbackEventType.EXPLICIT_COMMAND,
        normalized_signal=ExplicitCommand.FALSE_POSITIVE.value,
        signal_strength=SignalStrength.STRONG,
        source=FeedbackSource.REPLY_SYNC,
    )
    summary = compute_finding_assessment(_FINDING_ID, [explicit])
    assert is_false_positive_candidate(summary)


def test_high_value_candidate_true_when_finding_disappeared() -> None:
    event = _event(
        FeedbackEventType.FINDING_DISAPPEARED, signal_strength=SignalStrength.MEDIUM, source=FeedbackSource.REVIEW_MEMORY
    )
    summary = compute_finding_assessment(_FINDING_ID, [event])
    assert is_high_value_candidate(summary)


def test_high_value_candidate_false_for_a_single_positive_reaction_alone() -> None:
    pos = _event(
        FeedbackEventType.REACTION_ADDED,
        normalized_signal=NormalizedReactionHint.POSITIVE_HINT.value,
        metadata={"reaction_id": "1"},
    )
    summary = compute_finding_assessment(_FINDING_ID, [pos])
    assert not is_high_value_candidate(summary)
