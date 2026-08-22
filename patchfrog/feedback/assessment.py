"""Deterministic feedback interpretation -- the only place raw
:class:`~patchfrog.feedback.domain.FeedbackEvent` rows are turned into a
judgment about a finding.

Every rule here is explicit and traceable (see
:attr:`~patchfrog.feedback.domain.FeedbackAssessment.reasons`) -- there is
no weighted score, no machine learning, no LLM. The two rules that matter
most, straight from the Phase 9 core principle:

- A reaction (however many) only ever moves ``usefulness_signal``.
  ``correctness_signal`` is untouched by reactions -- only an explicit
  ``/patchfrog`` command or a Phase 7 code-lifecycle signal can move it,
  and even then only to a *signal*, never a proof.
- ``resolution_signal`` reflects whether a GitHub thread is open/closed,
  nothing about whether the finding was right.
"""

from __future__ import annotations

from collections.abc import Sequence
from uuid import UUID

from patchfrog.feedback.domain import (
    ExplicitCommand,
    FeedbackAssessment,
    FeedbackEvent,
    FeedbackEventType,
    FindingFeedbackSummary,
    NormalizedReactionHint,
    ResolutionState,
    SignalPolarity,
    SignalStrength,
)

_STRENGTH_RANK: dict[SignalStrength, int] = {
    SignalStrength.WEAK: 0,
    SignalStrength.MEDIUM: 1,
    SignalStrength.STRONG: 2,
}

_SCORED_EVENT_TYPES = frozenset(
    {
        FeedbackEventType.REACTION_ADDED,
        FeedbackEventType.REACTION_REMOVED,
        FeedbackEventType.COMMENT_REPLY,
        FeedbackEventType.EXPLICIT_COMMAND,
        FeedbackEventType.THREAD_RESOLVED,
        FeedbackEventType.THREAD_REOPENED,
        FeedbackEventType.FINDING_CODE_CHANGED,
        FeedbackEventType.FINDING_CODE_UNCHANGED,
        FeedbackEventType.FINDING_DISAPPEARED,
    }
)


def _strongest(strengths: Sequence[SignalStrength]) -> SignalStrength | None:
    if not strengths:
        return None
    return max(strengths, key=lambda s: _STRENGTH_RANK[s])


def _net_active_reactions(events: Sequence[FeedbackEvent]) -> dict[str, NormalizedReactionHint]:
    """Nets ``REACTION_ADDED``/``REACTION_REMOVED`` history (joined on the
    ``reaction_id`` each carries in ``metadata``) into the currently-active
    reaction set. A reaction added then later removed contributes nothing
    -- see Phase 9 spec section 14: "Do not keep a removed thumbs-up as
    permanently active feedback."
    """

    active: dict[str, NormalizedReactionHint] = {}
    for event in sorted(events, key=lambda e: e.occurred_at):
        reaction_id = event.metadata.get("reaction_id")
        if reaction_id is None:
            continue
        if event.event_type is FeedbackEventType.REACTION_ADDED:
            active[reaction_id] = NormalizedReactionHint(event.normalized_signal)
        elif event.event_type is FeedbackEventType.REACTION_REMOVED:
            active.pop(reaction_id, None)
    return active


def compute_finding_assessment(
    finding_id: UUID, events: Sequence[FeedbackEvent]
) -> FindingFeedbackSummary:
    """Deterministically derive one finding's :class:`FindingFeedbackSummary`
    from its complete raw event history. Pure function -- callers supply
    every relevant event; this never queries a database itself (see
    :mod:`patchfrog.feedback.queries` for that)."""

    active_reactions = _net_active_reactions(events)
    positive_reactions = sum(
        1 for h in active_reactions.values() if h is NormalizedReactionHint.POSITIVE_HINT
    )
    negative_reactions = sum(
        1 for h in active_reactions.values() if h is NormalizedReactionHint.NEGATIVE_HINT
    )

    developer_replies = sum(1 for e in events if e.event_type is FeedbackEventType.COMMENT_REPLY)

    def _explicit_count(command: ExplicitCommand) -> int:
        return sum(
            1
            for e in events
            if e.event_type is FeedbackEventType.EXPLICIT_COMMAND and e.normalized_signal == command.value
        )

    explicit_useful = _explicit_count(ExplicitCommand.USEFUL)
    explicit_false_positive = _explicit_count(ExplicitCommand.FALSE_POSITIVE)
    explicit_fixed = _explicit_count(ExplicitCommand.FIXED)
    explicit_ignore = _explicit_count(ExplicitCommand.IGNORE)

    thread_events = [
        e
        for e in events
        if e.event_type in (FeedbackEventType.THREAD_RESOLVED, FeedbackEventType.THREAD_REOPENED)
    ]
    thread_resolved = False
    if thread_events:
        latest_thread = max(thread_events, key=lambda e: e.occurred_at)
        thread_resolved = latest_thread.event_type is FeedbackEventType.THREAD_RESOLVED

    lifecycle_events = [
        e
        for e in events
        if e.event_type
        in (
            FeedbackEventType.FINDING_CODE_CHANGED,
            FeedbackEventType.FINDING_CODE_UNCHANGED,
            FeedbackEventType.FINDING_DISAPPEARED,
        )
    ]
    finding_changed = any(e.event_type is FeedbackEventType.FINDING_CODE_CHANGED for e in lifecycle_events)
    finding_disappeared = any(
        e.event_type is FeedbackEventType.FINDING_DISAPPEARED for e in lifecycle_events
    )
    latest_lifecycle = max(lifecycle_events, key=lambda e: e.occurred_at) if lifecycle_events else None
    # Positive confirmation of "unchanged" -- distinct from merely
    # "no FINDING_CODE_CHANGED event exists" (which could just mean no
    # recheck ever happened). Only the *latest* lifecycle observation
    # counts, so an earlier unchanged snapshot followed by a later
    # change/disappearance is never misread as still-unchanged.
    finding_code_unchanged = (
        latest_lifecycle is not None and latest_lifecycle.event_type is FeedbackEventType.FINDING_CODE_UNCHANGED
    )

    reasons: list[str] = []

    # -- usefulness_signal: explicit commands, then reactions. Never
    # anything else. --
    if explicit_useful > 0:
        usefulness_signal = SignalPolarity.POSITIVE
        reasons.append("explicit /patchfrog useful command")
    elif explicit_false_positive > 0:
        usefulness_signal = SignalPolarity.NEGATIVE
        reasons.append("explicit /patchfrog false-positive command")
    elif positive_reactions > negative_reactions and positive_reactions > 0:
        usefulness_signal = SignalPolarity.POSITIVE
        reasons.append(f"{positive_reactions} positive reaction(s) outweigh {negative_reactions} negative")
    elif negative_reactions > positive_reactions and negative_reactions > 0:
        usefulness_signal = SignalPolarity.NEGATIVE
        reasons.append(f"{negative_reactions} negative reaction(s) outweigh {positive_reactions} positive")
    elif positive_reactions or negative_reactions:
        usefulness_signal = SignalPolarity.NEUTRAL
        reasons.append("equal positive and negative reactions")
    else:
        usefulness_signal = SignalPolarity.UNKNOWN

    # -- correctness_signal: reactions never move this field. Only an
    # explicit command or a Phase 7 code-lifecycle signal can, and even
    # then only as a signal, never proof. --
    if explicit_false_positive > 0:
        correctness_signal = SignalPolarity.NEGATIVE
        reasons.append("explicit /patchfrog false-positive command")
    elif explicit_fixed > 0:
        correctness_signal = SignalPolarity.POSITIVE
        reasons.append("explicit /patchfrog fixed command")
    elif latest_lifecycle is not None and latest_lifecycle.event_type is FeedbackEventType.FINDING_DISAPPEARED:
        correctness_signal = SignalPolarity.POSITIVE
        reasons.append("finding disappeared on a later commit (medium-confidence signal, not confirmed)")
    elif (
        latest_lifecycle is not None
        and latest_lifecycle.event_type is FeedbackEventType.FINDING_CODE_UNCHANGED
        and negative_reactions > 0
    ):
        correctness_signal = SignalPolarity.NEGATIVE
        reasons.append("code unchanged across a later commit despite negative reaction(s)")
    else:
        correctness_signal = SignalPolarity.UNKNOWN

    # -- resolution_signal: open/closed only, never correctness. --
    if thread_events:
        resolution_signal = ResolutionState.CLOSED if thread_resolved else ResolutionState.OPEN
        reasons.append("thread resolved" if thread_resolved else "thread reopened/open")
    else:
        resolution_signal = ResolutionState.UNKNOWN

    engagement_signal = developer_replies > 0

    confidence = _strongest([e.signal_strength for e in events if e.event_type in _SCORED_EVENT_TYPES])

    assessment = FeedbackAssessment(
        finding_id=finding_id,
        usefulness_signal=usefulness_signal,
        correctness_signal=correctness_signal,
        resolution_signal=resolution_signal,
        engagement_signal=engagement_signal,
        confidence=confidence,
        reasons=tuple(reasons),
    )

    return FindingFeedbackSummary(
        finding_id=finding_id,
        positive_reactions=positive_reactions,
        negative_reactions=negative_reactions,
        developer_replies=developer_replies,
        explicit_useful=explicit_useful,
        explicit_false_positive=explicit_false_positive,
        explicit_fixed=explicit_fixed,
        explicit_ignore=explicit_ignore,
        thread_resolved=thread_resolved,
        finding_changed=finding_changed,
        finding_disappeared=finding_disappeared,
        finding_code_unchanged=finding_code_unchanged,
        assessment=assessment,
    )


def is_false_positive_candidate(summary: FindingFeedbackSummary) -> bool:
    """A finding with the strongest available negative evidence -- never
    ``confirmed_false_positive`` unless an explicit structured command
    says so (Phase 9 spec section 25).

    "Negative feedback + unchanged code" requires *positive confirmation*
    that a later recheck found the code unchanged
    (``finding_code_unchanged``) -- a single negative reaction with no
    recheck signal at all is not, by itself, enough evidence; that would
    contradict the core principle that a thumbs-down alone never proves a
    finding wrong. Two or more negative reactions is a separate,
    independently sufficient bar."""

    if summary.explicit_false_positive > 0:
        return True
    if summary.negative_reactions >= 2:
        return True
    return bool(summary.negative_reactions > 0 and summary.finding_code_unchanged)


def is_high_value_candidate(summary: FindingFeedbackSummary) -> bool:
    """A finding with the strongest available positive evidence -- never
    ``guaranteed_true_positive`` (Phase 9 spec section 26)."""

    if summary.explicit_useful > 0:
        return True
    if summary.finding_disappeared:
        return True
    return bool(summary.positive_reactions > 0 and summary.finding_changed)
