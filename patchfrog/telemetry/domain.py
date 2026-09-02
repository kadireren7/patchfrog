"""Pure domain model for PatchFrog's Evaluation & Telemetry Intelligence
layer.

Three distinct concepts, never conflated -- see ``docs/telemetry-intelligence.md``
for the full writeup:

- **Operational telemetry** (this package): what PatchFrog actually did
  -- structured metadata/provenance/outcomes reconstructed from already-
  persisted review/context/feedback state. Never a second warehouse of
  raw private customer code.
- **User feedback** (:mod:`patchfrog.feedback`): real-world reactions/
  commands. Noisy evidence, never canonical truth (see that package's own
  module docstring) -- a thumbs-up is never proof of correctness, and
  missing feedback is never proof of approval.
- **Benchmark ground truth** (:mod:`patchfrog.evaluation`): human-
  authored expected findings in evaluation fixtures. The only source of
  TP/FP/precision/recall. An LLM critic verdict is never benchmark truth.

These three are never mixed into one combined score anywhere in this
package.

**Privacy.** Every field on every dataclass below is structured metadata
-- an id, a count, an enum, a token/latency number, a category/severity/
role/tier, at most a file path + line range. Nothing here ever carries
raw source file content, full diff text, raw prompts, raw context
snippets, quoted evidence text, or API response bodies. Telemetry
references existing persisted review/context/feedback entities by their
stable ids; it is never a second copy of the code those entities are
about. See ``tests/unit/test_telemetry_reporting.py`` and
``tests/integration/test_telemetry_collector.py`` (the
``*_no_secret*``/``*redaction*`` tests) for the enforced guarantee.

Nothing in this module does I/O, calls an LLM, or opens a database
session -- that all lives in :mod:`patchfrog.telemetry.collector`.
Mirrors every other engine's own ``domain.py`` role (see
:mod:`patchfrog.review.domain`, :mod:`patchfrog.evaluation.domain`,
:mod:`patchfrog.feedback.domain`).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from uuid import UUID

from patchfrog.analysis.domain import Confidence, FindingCategory, Severity
from patchfrog.context.domain import ExpansionDirection, ExpansionReason
from patchfrog.feedback.domain import (
    FeedbackEventType,
    FeedbackSource,
    ResolutionState,
    SignalPolarity,
    SignalStrength,
)
from patchfrog.review.agents.roles import AgentRole
from patchfrog.review.domain import (
    CriticDecision,
    ProposalStatus,
    ReviewCandidateReason,
    ValidationOutcome,
)
from patchfrog.review.effort_types import ReviewEffortReason, ReviewEffortTier

#: Bumped whenever the JSON export shape changes materially -- consumers
#: (CI, future dashboards, ad-hoc scripts) key off this, never off
#: parsing prose. See :mod:`patchfrog.telemetry.reporting`.
#:
#: Bumped 1 -> 2 for Change Intelligence Foundation: ``ReviewTelemetrySnapshot``
#: gained the ``change_intelligence`` field
#: (:class:`ChangeIntelligenceTelemetry`), and :func:`patchfrog.telemetry.reporting.snapshot_to_dict`
#: exports every dataclass field via ``dataclasses.asdict`` -- so this is a
#: real exported-JSON-shape change, not an internal-only addition, even
#: though it's purely additive (no field removed/reinterpreted) and every
#: historical row exports it with explicit zero/default values (see
#: :func:`patchfrog.telemetry.collector.collect_review_telemetry`).
TELEMETRY_SCHEMA_VERSION = 2


class FindingLifecycleOutcome(StrEnum):
    """One proposal's final telemetry disposition -- always exactly one
    per proposal, derived deterministically from its already-persisted
    ``status``/critic decision, never inferred from prose. See
    :func:`classify_lifecycle_outcome`.
    """

    #: Not yet resolved to a terminal outcome. Never produced by
    #: :func:`classify_lifecycle_outcome` for a real persisted proposal
    #: (every persisted ``AIFindingProposalModel`` row already has a
    #: terminal ``status``) -- kept as an explicit, labeled bucket for a
    #: defensive/partial input rather than raising, and so every outcome
    #: named in the milestone spec has a member here.
    PROPOSED = "proposed"
    VALIDATION_REJECTED = "validation_rejected"
    CRITIC_REJECTED = "critic_rejected"
    CRITIC_DOWNGRADED = "critic_downgraded"
    SUPPRESSED_DUPLICATE = "suppressed_duplicate"
    SUPPRESSED_CONTRADICTION = "suppressed_contradiction"
    SUPPRESSED_BUDGET = "suppressed_budget"
    BELOW_CONFIDENCE_THRESHOLD = "below_confidence_threshold"
    ACCEPTED_FINAL = "accepted_final"


_STATUS_TO_OUTCOME: dict[ProposalStatus, FindingLifecycleOutcome] = {
    ProposalStatus.REJECTED_VALIDATION: FindingLifecycleOutcome.VALIDATION_REJECTED,
    ProposalStatus.REJECTED_CRITIC: FindingLifecycleOutcome.CRITIC_REJECTED,
    ProposalStatus.REJECTED_LOW_CONFIDENCE: FindingLifecycleOutcome.BELOW_CONFIDENCE_THRESHOLD,
    ProposalStatus.SUPPRESSED_DUPLICATE: FindingLifecycleOutcome.SUPPRESSED_DUPLICATE,
    ProposalStatus.SUPPRESSED_CONTRADICTION: FindingLifecycleOutcome.SUPPRESSED_CONTRADICTION,
    ProposalStatus.SUPPRESSED_BUDGET: FindingLifecycleOutcome.SUPPRESSED_BUDGET,
}


def classify_lifecycle_outcome(
    *, status: ProposalStatus, critic_decision: CriticDecision | None
) -> FindingLifecycleOutcome:
    """Deterministic mapping from a persisted proposal's terminal
    ``status`` (plus, only for ``ACCEPTED``, whether the critic
    downgraded it) to exactly one :class:`FindingLifecycleOutcome`.

    Every input here is an already-typed, already-persisted enum value
    -- this never parses ``validation_detail``/``reasoning_summary``
    prose, and it never re-derives a decision PatchFrog already made.
    """

    if status is ProposalStatus.ACCEPTED:
        if critic_decision is CriticDecision.DOWNGRADE:
            return FindingLifecycleOutcome.CRITIC_DOWNGRADED
        return FindingLifecycleOutcome.ACCEPTED_FINAL
    return _STATUS_TO_OUTCOME.get(status, FindingLifecycleOutcome.PROPOSED)


@dataclass(frozen=True, slots=True)
class FindingLifecycleTelemetry:
    """One proposal's provenance and final disposition. No message/
    reasoning/impact/suggested-fix/evidence text -- see the module
    docstring's privacy section and spec section 28 ("finding content
    minimization"). ``file_path``/line range are repository metadata,
    documented here as excludable later for external/aggregated
    telemetry (spec section 28)."""

    proposal_id: UUID
    candidate_id: UUID
    finding_id: UUID | None
    agent_role: AgentRole | None
    category: FindingCategory
    severity: Severity
    confidence: Confidence
    file_path: str
    start_line: int
    end_line: int
    #: The proposal's own deterministic validation outcome. ``None``
    #: only for rows persisted before this was captured (see migration
    #: 0017) -- never fabricated or inferred from prose.
    validation_outcome: ValidationOutcome | None
    status: ProposalStatus
    critic_decision: CriticDecision | None
    outcome: FindingLifecycleOutcome
    effort_tier: ReviewEffortTier | None


@dataclass(frozen=True, slots=True)
class ProviderRoleUsage:
    """One specialist role's reviewer usage within one run."""

    role: AgentRole
    calls: int
    input_tokens: int
    output_tokens: int
    thinking_tokens: int


@dataclass(frozen=True, slots=True)
class ProviderTelemetry:
    """Reviewer and critic provider usage, kept as two clearly separate
    sections (spec section 10) -- never summed together into one
    ambiguous "provider usage" number.

    ``*_latency_ms_aggregate`` fields are provider-work latency sums,
    **never** wall-clock measurements: specialist roles run concurrently
    within a candidate, and critic verifications run concurrently within
    a candidate too, so either aggregate can legitimately exceed the
    run's own wall-clock ``duration_ms``
    (:attr:`ReviewTelemetrySnapshot.duration_ms`). See
    :attr:`patchfrog.review.domain.ReviewRunSummary.reviewer_latency_ms`.
    """

    reviewer_provider: str
    reviewer_model: str
    critic_provider: str | None
    critic_model: str | None
    reviewer_calls_total: int
    reviewer_input_tokens_total: int
    reviewer_output_tokens_total: int
    reviewer_thinking_tokens_total: int
    reviewer_by_role: tuple[ProviderRoleUsage, ...]
    reviewer_latency_ms_aggregate: float
    critic_calls_total: int
    critic_input_tokens_total: int
    critic_output_tokens_total: int
    critic_thinking_tokens_total: int
    critic_latency_ms_aggregate: float
    retries_consumed: int


@dataclass(frozen=True, slots=True)
class CandidateTelemetry:
    """One review candidate's Quality + Cost Guard provenance and
    execution shape. No code content -- see the module docstring."""

    candidate_id: UUID
    file_path: str
    reason: ReviewCandidateReason
    status: str
    effort_tier: ReviewEffortTier | None
    effort_reasons: tuple[ReviewEffortReason, ...]
    escalated: bool
    escalation_reason: ReviewEffortReason | None
    context_bundle_id: UUID | None
    static_finding_count: int
    proposals_count: int
    accepted_count: int


@dataclass(frozen=True, slots=True)
class ContextTelemetry:
    """One context bundle's adaptive-expansion provenance and cost --
    never the bundle's snippet content (spec section 9/26)."""

    bundle_id: UUID
    candidate_id: UUID | None
    engine_version: int
    total_tokens: int
    total_lines: int
    generation_ms: float | None
    #: ``False`` both when adaptive mode was never requested for this
    #: bundle (fixed depth-1/depth-2) and for a bundle predating the
    #: Adaptive Context milestone -- exactly what
    #: :attr:`~patchfrog.persistence.models.context.ContextBundleModel.adaptive_expansion_attempted`
    #: stores, never fabricated into a three-way distinction telemetry
    #: doesn't need.
    adaptive_attempted: bool
    adaptive_occurred: bool
    adaptive_reasons: tuple[ExpansionReason, ...]
    adaptive_direction: ExpansionDirection | None
    depth_2_candidate_count: int
    depth_2_selected_count: int
    depth_2_tokens: int


class FeedbackScope(StrEnum):
    """Whether one piece of feedback telemetry is attributable to an
    exact published finding, or only to the review as a whole.

    :mod:`patchfrog.feedback`'s own attribution is deliberately
    best-effort (see :mod:`patchfrog.feedback.attribution`) --
    :attr:`~patchfrog.feedback.domain.FeedbackEvent.finding_id` is
    ``None`` whenever a raw signal (a reaction, a reply, an explicit
    command) could not be resolved to one exact finding. Telemetry must
    preserve that ambiguity, never force an unattributed signal onto a
    finding it was never confirmed to be about (spec section 33)."""

    FINDING = "finding"
    REVIEW = "review"


@dataclass(frozen=True, slots=True)
class FeedbackTelemetry:
    """One published finding's real-world feedback, if any --
    deliberately never re-interpreted here (spec section 12): a
    ``usefulness_signal`` of ``NEGATIVE`` is
    ``user_reported_false_positive``-shaped evidence, never a canonical
    false positive. ``has_feedback=False`` means "unknown", never
    "confirmed correct". Always :attr:`FeedbackScope.FINDING` --
    unattributed feedback is never represented here, see
    :class:`ReviewFeedbackEventTelemetry`."""

    finding_id: UUID
    has_feedback: bool
    usefulness_signal: SignalPolarity | None
    resolution_signal: ResolutionState | None
    explicit_useful: int
    explicit_false_positive: int
    explicit_fixed: int
    explicit_ignore: int
    positive_reactions: int
    negative_reactions: int
    scope: FeedbackScope = FeedbackScope.FINDING


@dataclass(frozen=True, slots=True)
class ReviewFeedbackEventTelemetry:
    """One raw feedback event that :mod:`patchfrog.feedback.attribution`
    could not attribute to an exact finding
    (``FeedbackEvent.finding_id is None``) -- still retained here for
    audit, never discarded and never forced onto any individual finding
    (spec sections 33/34).

    Deliberately no ``finding_id`` field at all: this dataclass can only
    ever represent :attr:`FeedbackScope.REVIEW`, so there is structurally
    nothing to misattribute. Privacy-safe by construction --
    :mod:`patchfrog.feedback.sync` never writes a reply/comment body into
    ``raw_signal``/``normalized_signal``/``metadata`` in the first place
    (``raw_signal`` is always an enum-shaped value like a reaction
    content or explicit-command token, or the empty string for a plain
    engagement signal), so nothing here needs scrubbing beyond simply not
    including fields this dataclass never had.
    """

    scope: FeedbackScope
    event_type: FeedbackEventType
    source: FeedbackSource
    normalized_signal: str
    signal_strength: SignalStrength
    occurred_at: str


@dataclass(frozen=True, slots=True)
class ChangeIntelligenceTelemetry:
    """Bounded, privacy-safe counts from
    :mod:`patchfrog.change_intelligence` for one review run (spec
    section 20). Deliberately counts only -- no Change Story prose, no
    Change Map text, no per-node evidence strings, even though those
    already-bounded strings are separately persisted on ``review_runs``
    for publication (:mod:`patchfrog.publishing`); telemetry is a
    structured-metadata surface, never a second copy of rendered text.
    """

    change_unit_count: int
    #: ``(kind_value, count)`` pairs, sorted by kind value -- a tuple,
    #: not a dict, matching the immutability discipline of every other
    #: collection field in this module.
    change_kind_counts: tuple[tuple[str, int], ...]
    affected_surface_count: int
    expected_companion_count: int
    missing_companion_candidate_count: int
    change_map_rendered: bool
    change_map_node_count: int


@dataclass(frozen=True, slots=True)
class ReviewTelemetrySnapshot:
    """The complete, deterministic telemetry snapshot for one review run
    -- what :func:`patchfrog.telemetry.collector.collect_review_telemetry`
    produces. Same DB state in, same snapshot out; collecting it never
    mutates review state (spec section 20/42)."""

    schema_version: int
    review_run_id: UUID
    repository_id: UUID
    pull_request_id: UUID | None
    status: str
    commit_sha: str
    started_at: str
    completed_at: str | None
    #: Wall-clock duration of the whole run -- see
    #: :class:`ProviderTelemetry`'s docstring for why this is never
    #: conflated with a provider-work latency aggregate.
    duration_ms: float | None
    candidate_count: int
    candidates_reviewed: int
    candidates_failed: int
    candidates_skipped_budget: int
    candidates_escalated: int
    candidates: tuple[CandidateTelemetry, ...]
    finding_lifecycle: tuple[FindingLifecycleTelemetry, ...]
    provider: ProviderTelemetry
    context: tuple[ContextTelemetry, ...]
    feedback: tuple[FeedbackTelemetry, ...]
    #: Feedback events tied to this run but not attributable to one
    #: exact finding -- see :class:`ReviewFeedbackEventTelemetry`. Never
    #: folded into ``feedback`` above and never used by
    #: :func:`patchfrog.telemetry.aggregation.compute_feedback_coverage`
    #: (spec section 33/34).
    review_feedback: tuple[ReviewFeedbackEventTelemetry, ...] = ()
    #: All-zero/empty for a run that predates Change Intelligence
    #: Foundation -- same nullable-safe-default convention as
    #: ``candidates_by_tier``/``calls_by_role`` above, never a separate
    #: ``None`` sentinel. See :class:`ChangeIntelligenceTelemetry`.
    change_intelligence: ChangeIntelligenceTelemetry = field(
        default_factory=lambda: ChangeIntelligenceTelemetry(
            change_unit_count=0,
            change_kind_counts=(),
            affected_surface_count=0,
            expected_companion_count=0,
            missing_companion_candidate_count=0,
            change_map_rendered=False,
            change_map_node_count=0,
        )
    )


@dataclass(frozen=True, slots=True)
class TelemetryAggregate:
    """A plain, auditable sum of many :class:`ReviewTelemetrySnapshot`
    instances -- one review run, one repository's runs, or an arbitrary
    set of run ids (spec section 21). Deliberately just totals; slice
    breakdowns (by role/tier/category) are pure functions over the
    snapshots themselves (see :mod:`patchfrog.telemetry.aggregation`),
    never pre-baked into this dataclass, so this stays small and never
    goes stale relative to what the funnel/breakdown functions compute.
    """

    schema_version: int
    review_run_count: int
    candidate_count: int
    candidates_reviewed: int
    candidates_skipped_budget: int
    candidates_escalated: int
    proposals_count: int
    reviewer_calls_total: int
    reviewer_input_tokens_total: int
    reviewer_output_tokens_total: int
    reviewer_thinking_tokens_total: int
    reviewer_latency_ms_aggregate: float
    critic_calls_total: int
    critic_input_tokens_total: int
    critic_output_tokens_total: int
    critic_thinking_tokens_total: int
    critic_latency_ms_aggregate: float
    retries_consumed: int
