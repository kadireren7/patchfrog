"""Pure aggregation over one or many :class:`~patchfrog.telemetry.domain.ReviewTelemetrySnapshot`
instances.

No I/O, no database, no LLM -- every function here takes plain
snapshots/sequences and returns plain dataclasses/dicts, mirroring
:mod:`patchfrog.evaluation.metrics`'s own role for evaluation runs.
Prefer these pure functions over a database-specific analytics query
(spec section 21): aggregating across an arbitrary set of run ids is
just "collect each snapshot, then call these functions," never a second,
divergent SQL aggregation path.

Every count here is a plain, auditable tally -- never a composite score,
never a weighted rate that mixes telemetry with feedback or benchmark
ground truth (spec section 39: those stay strictly separate).
"""

from __future__ import annotations

import uuid
from collections import Counter, defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from patchfrog.feedback.domain import SignalPolarity
from patchfrog.telemetry.domain import (
    TELEMETRY_SCHEMA_VERSION,
    CandidateTelemetry,
    ContextTelemetry,
    FeedbackTelemetry,
    FindingLifecycleOutcome,
    FindingLifecycleTelemetry,
    ReviewFeedbackEventTelemetry,
    ReviewTelemetrySnapshot,
    TelemetryAggregate,
)

_UNKNOWN = "unknown"


def _rate(numerator: int, denominator: int) -> float:
    return numerator / denominator if denominator else 0.0


def aggregate_snapshots(snapshots: Sequence[ReviewTelemetrySnapshot]) -> TelemetryAggregate:
    """Sum many snapshots into one :class:`TelemetryAggregate` -- one
    review run, one repository's runs, or an arbitrary set of run ids
    (spec section 21). Deliberately just totals; use the slice/funnel
    functions below on the snapshots' own ``finding_lifecycle``/
    ``candidates``/``context``/``feedback`` tuples (concatenated across
    every snapshot, if needed) for any breakdown."""

    return TelemetryAggregate(
        schema_version=TELEMETRY_SCHEMA_VERSION,
        review_run_count=len(snapshots),
        candidate_count=sum(s.candidate_count for s in snapshots),
        candidates_reviewed=sum(s.candidates_reviewed for s in snapshots),
        candidates_skipped_budget=sum(s.candidates_skipped_budget for s in snapshots),
        candidates_escalated=sum(s.candidates_escalated for s in snapshots),
        proposals_count=sum(len(s.finding_lifecycle) for s in snapshots),
        reviewer_calls_total=sum(s.provider.reviewer_calls_total for s in snapshots),
        reviewer_input_tokens_total=sum(s.provider.reviewer_input_tokens_total for s in snapshots),
        reviewer_output_tokens_total=sum(s.provider.reviewer_output_tokens_total for s in snapshots),
        reviewer_thinking_tokens_total=sum(s.provider.reviewer_thinking_tokens_total for s in snapshots),
        reviewer_latency_ms_aggregate=sum(s.provider.reviewer_latency_ms_aggregate for s in snapshots),
        critic_calls_total=sum(s.provider.critic_calls_total for s in snapshots),
        critic_input_tokens_total=sum(s.provider.critic_input_tokens_total for s in snapshots),
        critic_output_tokens_total=sum(s.provider.critic_output_tokens_total for s in snapshots),
        critic_thinking_tokens_total=sum(s.provider.critic_thinking_tokens_total for s in snapshots),
        critic_latency_ms_aggregate=sum(s.provider.critic_latency_ms_aggregate for s in snapshots),
        retries_consumed=sum(s.provider.retries_consumed for s in snapshots),
    )


def compute_lifecycle_outcome_counts(entries: Sequence[FindingLifecycleTelemetry]) -> dict[str, int]:
    """Every :class:`~patchfrog.telemetry.domain.FindingLifecycleOutcome`
    value gets its own key -- suppression reasons are never merged into
    one number (spec section 7)."""

    return dict(Counter(e.outcome.value for e in entries))


def _group_counts(
    entries: Sequence[FindingLifecycleTelemetry],
    *,
    group_key: Callable[[FindingLifecycleTelemetry], str],
    value_key: Callable[[FindingLifecycleTelemetry], str | None],
) -> dict[str, dict[str, int]]:
    counts: dict[str, Counter[str]] = defaultdict(Counter)
    for entry in entries:
        value = value_key(entry)
        if value is None:
            continue
        counts[group_key(entry)][value] += 1
    return {group: dict(values) for group, values in counts.items()}


def _role_key(entry: FindingLifecycleTelemetry) -> str:
    return entry.agent_role.value if entry.agent_role is not None else _UNKNOWN


def _tier_key(entry: FindingLifecycleTelemetry) -> str:
    return entry.effort_tier.value if entry.effort_tier is not None else _UNKNOWN


def _category_key(entry: FindingLifecycleTelemetry) -> str:
    return entry.category.value


def _severity_key(entry: FindingLifecycleTelemetry) -> str:
    return entry.severity.value


def compute_validation_outcomes_by_role(entries: Sequence[FindingLifecycleTelemetry]) -> dict[str, dict[str, int]]:
    """Spec section 5: e.g. "is Security producing more unsupported
    claims than Correctness?" -- never labeled "false positive" here,
    only the deterministic :class:`~patchfrog.review.domain.ValidationOutcome`
    PatchFrog itself already computed."""

    return _group_counts(
        entries, group_key=_role_key, value_key=lambda e: e.validation_outcome.value if e.validation_outcome else None
    )


def compute_validation_outcomes_by_tier(entries: Sequence[FindingLifecycleTelemetry]) -> dict[str, dict[str, int]]:
    return _group_counts(
        entries, group_key=_tier_key, value_key=lambda e: e.validation_outcome.value if e.validation_outcome else None
    )


def compute_validation_outcomes_by_category(entries: Sequence[FindingLifecycleTelemetry]) -> dict[str, dict[str, int]]:
    return _group_counts(
        entries,
        group_key=_category_key,
        value_key=lambda e: e.validation_outcome.value if e.validation_outcome else None,
    )


def compute_critic_decisions_by_role(entries: Sequence[FindingLifecycleTelemetry]) -> dict[str, dict[str, int]]:
    """Spec section 6: critic accept/reject/downgrade by role. A critic
    ``reject`` here is never equivalent to a benchmark false positive --
    see the module docstring of :mod:`patchfrog.telemetry.domain`."""

    return _group_counts(
        entries, group_key=_role_key, value_key=lambda e: e.critic_decision.value if e.critic_decision else None
    )


def compute_critic_decisions_by_tier(entries: Sequence[FindingLifecycleTelemetry]) -> dict[str, dict[str, int]]:
    return _group_counts(
        entries, group_key=_tier_key, value_key=lambda e: e.critic_decision.value if e.critic_decision else None
    )


def compute_critic_decisions_by_severity(entries: Sequence[FindingLifecycleTelemetry]) -> dict[str, dict[str, int]]:
    return _group_counts(
        entries, group_key=_severity_key, value_key=lambda e: e.critic_decision.value if e.critic_decision else None
    )


@dataclass(frozen=True, slots=True)
class SliceFunnel:
    """One slice's proposal funnel -- spec sections 30/31: "At minimum:
    proposed, validation-valid, validation-rejected, critic-rejected,
    final." Counts every proposal exactly once (each proposal's
    ``outcome`` places it in exactly one bucket below)."""

    key: str
    proposed: int
    validation_valid: int
    validation_rejected: int
    critic_rejected: int
    accepted_final: int


def _funnel_for_group(key: str, entries: list[FindingLifecycleTelemetry]) -> SliceFunnel:
    return SliceFunnel(
        key=key,
        proposed=len(entries),
        validation_valid=sum(1 for e in entries if e.outcome is not FindingLifecycleOutcome.VALIDATION_REJECTED),
        validation_rejected=sum(1 for e in entries if e.outcome is FindingLifecycleOutcome.VALIDATION_REJECTED),
        critic_rejected=sum(1 for e in entries if e.outcome is FindingLifecycleOutcome.CRITIC_REJECTED),
        accepted_final=sum(
            1
            for e in entries
            if e.outcome in (FindingLifecycleOutcome.ACCEPTED_FINAL, FindingLifecycleOutcome.CRITIC_DOWNGRADED)
        ),
    )


def compute_role_funnel(entries: Sequence[FindingLifecycleTelemetry]) -> tuple[SliceFunnel, ...]:
    by_role: dict[str, list[FindingLifecycleTelemetry]] = defaultdict(list)
    for e in entries:
        by_role[_role_key(e)].append(e)
    return tuple(_funnel_for_group(key, by_role[key]) for key in sorted(by_role))


def compute_tier_funnel(entries: Sequence[FindingLifecycleTelemetry]) -> tuple[SliceFunnel, ...]:
    by_tier: dict[str, list[FindingLifecycleTelemetry]] = defaultdict(list)
    for e in entries:
        by_tier[_tier_key(e)].append(e)
    return tuple(_funnel_for_group(key, by_tier[key]) for key in sorted(by_tier))


@dataclass(frozen=True, slots=True)
class QualityFunnel:
    """The whole-run proposal funnel (spec section 29): candidates ->
    proposals -> valid proposals -> accepted/downgraded (post critic,
    post cross-role/AI dedup, post confidence) -> published -> feedback-
    bearing. ``drop_off`` breaks down every non-accepted outcome
    individually (never merged) and, together with ``accepted_final``,
    accounts for exactly ``proposals`` with no double-counting.

    ``published_findings`` in v1 means "persisted to ``ai_findings``",
    the terminal PatchFrog-internal disposition -- not "commented on
    GitHub," which is a separate, further-filtered downstream step (see
    ``patchfrog/publishing/``) this milestone does not have run-level
    telemetry for yet.
    """

    candidates: int
    proposals: int
    validation_valid: int
    accepted_final: int
    published_findings: int
    feedback_bearing_findings: int
    drop_off: dict[str, int] = field(default_factory=dict)


def compute_quality_funnel(
    *,
    candidate_count: int,
    entries: Sequence[FindingLifecycleTelemetry],
    feedback: Sequence[FeedbackTelemetry],
) -> QualityFunnel:
    outcome_counts = compute_lifecycle_outcome_counts(entries)
    accepted_final = outcome_counts.get(FindingLifecycleOutcome.ACCEPTED_FINAL.value, 0) + outcome_counts.get(
        FindingLifecycleOutcome.CRITIC_DOWNGRADED.value, 0
    )
    drop_off = {k: v for k, v in outcome_counts.items() if k not in (
        FindingLifecycleOutcome.ACCEPTED_FINAL.value, FindingLifecycleOutcome.CRITIC_DOWNGRADED.value,
    )}
    published_findings = sum(1 for e in entries if e.finding_id is not None)
    return QualityFunnel(
        candidates=candidate_count,
        proposals=len(entries),
        validation_valid=sum(
            1 for e in entries if e.outcome is not FindingLifecycleOutcome.VALIDATION_REJECTED
        ),
        accepted_final=accepted_final,
        published_findings=published_findings,
        feedback_bearing_findings=sum(1 for f in feedback if f.has_feedback),
        drop_off=drop_off,
    )


@dataclass(frozen=True, slots=True)
class TierDistribution:
    tier_counts: dict[str, int]
    escalated: int


def compute_tier_distribution(candidates: Sequence[CandidateTelemetry]) -> TierDistribution:
    counts: Counter[str] = Counter(c.effort_tier.value for c in candidates if c.effort_tier is not None)
    escalated = sum(1 for c in candidates if c.escalated)
    return TierDistribution(tier_counts=dict(counts), escalated=escalated)


@dataclass(frozen=True, slots=True)
class AdaptiveContextSummary:
    """Spec section 9. Purely observational counts -- never a claim of
    causal quality improvement (spec section 32)."""

    bundles: int
    attempted: int
    occurred: int
    reasons: dict[str, int]
    directions: dict[str, int]
    depth_2_candidate_count: int
    depth_2_selected_count: int
    depth_2_tokens: int
    total_tokens: int


def compute_adaptive_context_summary(context: Sequence[ContextTelemetry]) -> AdaptiveContextSummary:
    reasons: Counter[str] = Counter()
    directions: Counter[str] = Counter()
    for c in context:
        reasons.update(r.value for r in c.adaptive_reasons)
        if c.adaptive_direction is not None:
            directions[c.adaptive_direction] += 1
    return AdaptiveContextSummary(
        bundles=len(context),
        attempted=sum(1 for c in context if c.adaptive_attempted),
        occurred=sum(1 for c in context if c.adaptive_occurred),
        reasons=dict(reasons),
        directions=dict(directions),
        depth_2_candidate_count=sum(c.depth_2_candidate_count for c in context),
        depth_2_selected_count=sum(c.depth_2_selected_count for c in context),
        depth_2_tokens=sum(c.depth_2_tokens for c in context),
        total_tokens=sum(c.total_tokens for c in context),
    )


@dataclass(frozen=True, slots=True)
class ContextEffectivenessComparison:
    """Spec section 32: proposals/finals on expanded vs. non-expanded
    candidates -- observational only, never a causal-improvement claim."""

    expanded_candidates: int
    expanded_finals: int
    expanded_validation_rejected: int
    non_expanded_candidates: int
    non_expanded_finals: int
    non_expanded_validation_rejected: int


def compute_context_effectiveness(
    *,
    candidates: Sequence[CandidateTelemetry],
    context: Sequence[ContextTelemetry],
    entries: Sequence[FindingLifecycleTelemetry],
) -> ContextEffectivenessComparison:
    expanded_bundle_ids = {c.bundle_id for c in context if c.adaptive_occurred}
    expanded_candidate_ids = {
        c.candidate_id for c in candidates if c.context_bundle_id is not None and c.context_bundle_id in expanded_bundle_ids
    }
    non_expanded_candidate_ids = {c.candidate_id for c in candidates} - expanded_candidate_ids

    def _finals(ids: set[uuid.UUID]) -> int:
        return sum(
            1
            for e in entries
            if e.candidate_id in ids
            and e.outcome in (FindingLifecycleOutcome.ACCEPTED_FINAL, FindingLifecycleOutcome.CRITIC_DOWNGRADED)
        )

    def _rejected(ids: set[uuid.UUID]) -> int:
        return sum(
            1 for e in entries if e.candidate_id in ids and e.outcome is FindingLifecycleOutcome.VALIDATION_REJECTED
        )

    return ContextEffectivenessComparison(
        expanded_candidates=len(expanded_candidate_ids),
        expanded_finals=_finals(expanded_candidate_ids),
        expanded_validation_rejected=_rejected(expanded_candidate_ids),
        non_expanded_candidates=len(non_expanded_candidate_ids),
        non_expanded_finals=_finals(non_expanded_candidate_ids),
        non_expanded_validation_rejected=_rejected(non_expanded_candidate_ids),
    )


@dataclass(frozen=True, slots=True)
class FeedbackCoverage:
    """Spec section 13. Never a "global false-positive rate" -- every
    rate here is explicitly denominated by feedback-*bearing* findings,
    and missing feedback is never treated as either positive or negative
    signal (spec section 33/39)."""

    published_findings: int
    feedback_bearing_findings: int
    coverage_rate: float
    useful_rate: float
    user_reported_false_positive_rate: float
    fixed_rate: float


def compute_feedback_coverage(feedback: Sequence[FeedbackTelemetry]) -> FeedbackCoverage:
    published = len(feedback)
    bearing = [f for f in feedback if f.has_feedback]
    bearing_count = len(bearing)
    useful = sum(1 for f in bearing if f.usefulness_signal is SignalPolarity.POSITIVE or f.explicit_useful > 0)
    false_positive = sum(1 for f in bearing if f.explicit_false_positive > 0)
    fixed = sum(1 for f in bearing if f.explicit_fixed > 0)
    return FeedbackCoverage(
        published_findings=published,
        feedback_bearing_findings=bearing_count,
        coverage_rate=_rate(bearing_count, published),
        useful_rate=_rate(useful, bearing_count),
        user_reported_false_positive_rate=_rate(false_positive, bearing_count),
        fixed_rate=_rate(fixed, bearing_count),
    )


@dataclass(frozen=True, slots=True)
class ReviewFeedbackSummary:
    """Aggregate over review-scoped (unattributed) feedback events --
    spec sections 33/34. Deliberately separate from :class:`FeedbackCoverage`:
    these events never participate in any per-finding rate, and are never
    collapsed into one fabricated truth label -- every conflicting raw
    event is retained and counted individually in the breakdowns below."""

    review_feedback_event_count: int
    review_feedback_by_event_type: dict[str, int]
    review_feedback_by_signal: dict[str, int]


def compute_review_feedback_summary(
    events: Sequence[ReviewFeedbackEventTelemetry],
) -> ReviewFeedbackSummary:
    return ReviewFeedbackSummary(
        review_feedback_event_count=len(events),
        review_feedback_by_event_type=dict(Counter(e.event_type.value for e in events)),
        review_feedback_by_signal=dict(Counter(e.normalized_signal for e in events)),
    )
