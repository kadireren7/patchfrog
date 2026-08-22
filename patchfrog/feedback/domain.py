"""Pure domain model for PatchFrog's operational feedback loop (Phase 9).

Core principle: feedback is noisy evidence, not ground truth.

- A thumbs-down does not mean "the finding was wrong."
- A resolved thread does not mean "the finding was correct."
- A code change does not mean "the developer accepted the suggestion."

Every raw signal is kept distinct and mapped through explicit, auditable
rules in :mod:`patchfrog.feedback.assessment` -- never a mysterious
weighted ML score, never an LLM judge, never a signal that automatically
mutates a reviewer prompt, a policy threshold, or future finding
selection. This module has zero I/O and zero SQLAlchemy dependency,
mirroring every other engine's own ``domain.py`` role (see
:mod:`patchfrog.review.domain`, :mod:`patchfrog.publishing.domain`).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from patchfrog.domain.github_feedback import GitHubReactionContent

#: Bumped whenever the raw ingestion pipeline's own behavior changes
#: materially (new event types, new sync sources) -- never for a pure bug
#: fix. Raw ``FeedbackEventModel`` rows are immutable regardless of this
#: version; it exists for audit/debugging, not reinterpretation.
FEEDBACK_ENGINE_VERSION = 1

#: Bumped whenever the deterministic rules in
#: :mod:`patchfrog.feedback.assessment` change materially -- a derived
#: ``FeedbackAssessment``/``FindingFeedbackSummary`` computed under an
#: old version is never silently treated as equivalent to one computed
#: under a new version (see ``FeedbackAssessmentModel.assessment_version``
#: and :func:`patchfrog.feedback.assessment.recompute_all`).
FEEDBACK_ASSESSMENT_VERSION = 1


class FeedbackEventType(StrEnum):
    """Every kind of feedback signal Phase 9 observes. Deliberately
    excludes ``COMMENT_EDITED`` -- detecting a content change reliably
    without a webhook would require diffing bodies against a prior synced
    copy, which is out of scope for this phase (see docs/feedback.md)."""

    REACTION_ADDED = "reaction_added"
    REACTION_REMOVED = "reaction_removed"
    COMMENT_REPLY = "comment_reply"
    EXPLICIT_COMMAND = "explicit_command"
    THREAD_RESOLVED = "thread_resolved"
    THREAD_REOPENED = "thread_reopened"
    FINDING_CODE_CHANGED = "finding_code_changed"
    FINDING_CODE_UNCHANGED = "finding_code_unchanged"
    FINDING_DISAPPEARED = "finding_disappeared"
    PR_MERGED = "pr_merged"
    PR_CLOSED = "pr_closed"


class FeedbackSource(StrEnum):
    """Which sync path produced one event. Every source here is a polled
    GitHub sync run on demand via ``patchfrog.cli feedback sync`` --
    GitHub has no webhook event for reactions at all, and PatchFrog is not
    currently subscribed to ``pull_request_review_comment``/
    ``pull_request_review_thread`` (see docs/feedback.md's permissions
    audit), so nothing here is webhook-driven today."""

    REACTION_SYNC = "reaction_sync"
    REPLY_SYNC = "reply_sync"
    THREAD_SYNC = "thread_sync"
    PR_LIFECYCLE_SYNC = "pr_lifecycle_sync"
    REVIEW_MEMORY = "review_memory"


class SignalStrength(StrEnum):
    """How much weight one raw signal carries -- never mapped directly to
    "true" or "false" on its own (see :mod:`patchfrog.feedback.assessment`)."""

    WEAK = "weak"
    MEDIUM = "medium"
    STRONG = "strong"


class NormalizedReactionHint(StrEnum):
    """A GitHub reaction's normalized hint -- deliberately not a bare
    +1/-1: ``laugh``/``rocket`` are explicitly ``UNINTERPRETED``, never
    folded into positive/negative."""

    POSITIVE_HINT = "positive_hint"
    NEGATIVE_HINT = "negative_hint"
    NEUTRAL_ATTENTION = "neutral_attention"
    UNINTERPRETED = "uninterpreted"


_REACTION_HINTS: dict[GitHubReactionContent, NormalizedReactionHint] = {
    GitHubReactionContent.PLUS_ONE: NormalizedReactionHint.POSITIVE_HINT,
    GitHubReactionContent.HEART: NormalizedReactionHint.POSITIVE_HINT,
    GitHubReactionContent.HOORAY: NormalizedReactionHint.POSITIVE_HINT,
    GitHubReactionContent.MINUS_ONE: NormalizedReactionHint.NEGATIVE_HINT,
    GitHubReactionContent.CONFUSED: NormalizedReactionHint.NEGATIVE_HINT,
    GitHubReactionContent.EYES: NormalizedReactionHint.NEUTRAL_ATTENTION,
    GitHubReactionContent.LAUGH: NormalizedReactionHint.UNINTERPRETED,
    GitHubReactionContent.ROCKET: NormalizedReactionHint.UNINTERPRETED,
}


def normalize_reaction(content: GitHubReactionContent) -> NormalizedReactionHint:
    """Deterministic hint for one raw GitHub reaction. The raw ``content``
    is always preserved on the persisted event alongside this hint --
    never reduced to a bare +/-1 internally."""

    return _REACTION_HINTS[content]


class ExplicitCommand(StrEnum):
    """The exact, closed vocabulary of ``/patchfrog <token>`` reply
    commands Phase 9 recognizes -- see :mod:`patchfrog.feedback.commands`
    for the strict parser. No arguments, no other tokens, ever."""

    USEFUL = "useful"
    FALSE_POSITIVE = "false-positive"
    FIXED = "fixed"
    IGNORE = "ignore"


class SignalPolarity(StrEnum):
    """A generic positive/negative/neutral/unknown polarity, used by every
    field of :class:`FeedbackAssessment`."""

    POSITIVE = "positive"
    NEGATIVE = "negative"
    NEUTRAL = "neutral"
    UNKNOWN = "unknown"


class ResolutionState(StrEnum):
    OPEN = "open"
    CLOSED = "closed"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class ActorIdentity:
    """Minimal actor identity persisted with a feedback event -- login and
    whether it is a bot, nothing more. No profile, no cross-repository
    correlation, no reputation tracking (see docs/feedback.md's privacy
    section)."""

    login: str
    is_bot: bool


@dataclass(frozen=True, slots=True)
class FeedbackEvent:
    """One raw, append-only feedback signal -- never mutated or deleted
    once ingested. Attribution (``finding_id``/
    ``review_publication_comment_id``) is best-effort: ``None`` on either
    means this event could not be attributed to an exact PatchFrog
    finding (see :mod:`patchfrog.feedback.attribution`) -- it is still
    recorded, never discarded, for audit, but excluded from any
    per-finding aggregation."""

    repository_id: UUID
    pull_request_id: UUID | None
    review_run_id: UUID | None
    publication_id: UUID | None
    review_publication_comment_id: UUID | None
    finding_id: UUID | None
    github_review_id: int | None
    github_comment_id: int | None
    event_type: FeedbackEventType
    source: FeedbackSource
    external_event_id: str
    raw_signal: str
    normalized_signal: str
    signal_strength: SignalStrength
    actor: ActorIdentity
    occurred_at: datetime
    metadata: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class FeedbackAssessment:
    """The deterministic, rule-based interpretation of one finding's raw
    feedback events at one point in time -- never a weighted ML score.
    ``reasons`` traces exactly which raw signals drove each field, so a
    human can always audit *why* this assessment came out the way it did."""

    finding_id: UUID
    usefulness_signal: SignalPolarity
    correctness_signal: SignalPolarity
    resolution_signal: ResolutionState
    engagement_signal: bool
    confidence: SignalStrength | None
    reasons: tuple[str, ...]
    assessment_version: int = FEEDBACK_ASSESSMENT_VERSION


@dataclass(frozen=True, slots=True)
class FindingFeedbackSummary:
    """Aggregated counts plus the derived :class:`FeedbackAssessment` for
    one finding. Every count here is a plain, auditable tally, never a
    composite score."""

    finding_id: UUID
    positive_reactions: int
    negative_reactions: int
    developer_replies: int
    explicit_useful: int
    explicit_false_positive: int
    explicit_fixed: int
    explicit_ignore: int
    thread_resolved: bool
    finding_changed: bool
    finding_disappeared: bool
    #: Positive confirmation that a later Phase 7 recheck found this
    #: finding's evidence unchanged -- distinct from simply "no
    #: FINDING_CODE_CHANGED event was observed" (which could just mean no
    #: recheck happened at all). See
    #: :func:`patchfrog.feedback.assessment.is_false_positive_candidate`:
    #: "negative feedback + unchanged code" requires this to actually be
    #: ``True``, never merely the absence of a changed signal.
    finding_code_unchanged: bool
    assessment: FeedbackAssessment
