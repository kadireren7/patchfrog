"""Review Quality + Cost Guard: deterministic per-candidate effort
tiering.

"The model may propose. PatchFrog decides what survives." Extended by
this milestone: "PatchFrog decides how much model work is justified."
Every candidate gets exactly one :class:`ReviewEffortTier` before any
specialist provider call, decided purely from already-known structural/
static signals -- never an LLM router, never NLP over comments, never a
repository path/name keyword used as the *sole* decisive signal.

The tier controls execution shape only: which specialist roles run, how
much of the existing context/output/retry budget a candidate gets, and
how strict critic verification must be. It never controls provider,
model, critic model, or credentials -- those remain exclusively
operator-controlled (:mod:`patchfrog.review.runtime_config`, Milestone
C), completely untouched by this module.

Three-stage decision, resolving two real ordering dependencies (context
budget depends on tier, and one tier signal -- adaptive depth-2 evidence
-- only exists *after* context is built; a proposal's own risk profile
only exists *after* specialist calls return):

1. :meth:`ReviewEffortPolicy.decide_provisional` -- before context is
   built, using only the candidate and its attached static findings.
   Determines the context budget/adaptive-mode policy used to build
   context.
2. :meth:`ReviewEffortPolicy.finalize` -- after context is built, before
   any specialist provider call. May escalate (never de-escalate) the
   provisional tier to DEEP if adaptive expansion actually occurred.
3. :meth:`ReviewEffortPolicy.escalate_for_high_risk_proposal` -- after
   specialist proposals are validated and cross-role-grouped, before
   critic verification. May escalate (never de-escalate) to DEEP if a
   surviving proposal carries a deterministic high-risk signal
   (HIGH/CRITICAL severity, security category, or unresolved-
   contradiction membership) -- the path a LIGHT candidate actually
   reaches in practice, since LIGHT disables adaptive context and so
   can never trigger stage 2's escalation.

Each candidate escalates **at most once total** across stages 2 and 3
combined: both stages guard on ``tier is DEEP`` (the tier ceiling), so a
candidate that started at DEEP, escalated to DEEP at stage 2, or already
escalated to DEEP at stage 3 always short-circuits any further
escalation attempt -- never a second traversal, never a repeated
decision loop, never an LLM-based router deciding either.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from patchfrog.analysis.domain import FindingCategory, Severity
from patchfrog.review.agents.roles import AgentRole
from patchfrog.review.agents.selection import (
    AgentSelectionPolicy,
    AgentSelectionReason,
)
from patchfrog.review.domain import ReviewCandidate, StaticFindingSummary
from patchfrog.review.effort_types import (
    CriticExpectation,
    ReviewEffortReason,
    ReviewEffortTier,
)

#: Categories where a direct static finding already signals a review
#: that shouldn't be treated as routine -- mirrors the category tables
#: already established in :mod:`patchfrog.context.scoring` and
#: :mod:`patchfrog.context.adaptive` rather than inventing a third one.
_HIGH_RISK_STATIC_CATEGORIES = frozenset(
    {
        FindingCategory.SECURITY,
        FindingCategory.MEMORY_SAFETY,
        FindingCategory.RESOURCE_MANAGEMENT,
        FindingCategory.CONCURRENCY,
    }
)

#: A changed-symbol span (in lines) beyond this is "large" -- a coarse,
#: explainable structural-complexity signal, not a quality judgment.
_LARGE_CHANGED_SYMBOL_LINES = 80
#: More changed lines than this within one candidate is "substantial."
_MANY_CHANGED_LINES = 15
#: Two or more otherwise-unremarkable signals together still warrant
#: more than LIGHT effort -- corroboration, not any single weak signal.
_MULTIPLE_SIGNALS_THRESHOLD = 2


@dataclass(frozen=True, slots=True)
class ReviewEffortDecision:
    """Immutable, auditable per-candidate execution plan -- determined
    before any specialist provider call. See module docstring for the
    two-stage (provisional -> finalized) decision process.

    Never contains a provider, model, or credential -- see
    :mod:`patchfrog.review.runtime_config` for that operator-controlled
    concern, entirely orthogonal to this decision.
    """

    tier: ReviewEffortTier
    reasons: tuple[ReviewEffortReason, ...]
    selected_roles: frozenset[AgentRole]
    #: Fraction of the per-candidate context ``max_tokens``/``max_lines``
    #: ceiling this tier gets -- never > 1.0, so no tier can ever exceed
    #: the operator/repository-configured ceiling.
    context_token_fraction: float
    context_adaptive_enabled: bool
    critic_expectation: CriticExpectation
    #: Never exceeds the effective ``ReviewConfig.max_retries`` -- a
    #: tier may only *reduce* retry allowance, never grant more than
    #: configured.
    retry_limit: int
    #: Fraction of ``max_output_tokens_per_candidate`` (the shared,
    #: candidate-level ceiling -- see module docstring of
    #: :mod:`patchfrog.review.orchestration`) EACH selected role gets,
    #: independent of how many roles are selected -- deliberately not a
    #: candidate-level fraction subsequently divided by role count, which
    #: would let a single-role LIGHT candidate's one role receive as much
    #: (or more) per-role budget as a two-role STANDARD/DEEP candidate's
    #: role, inverting the intended LIGHT < STANDARD <= DEEP ordering
    #: (spec section 7). Chosen so total candidate spend
    #: (``fraction * len(selected_roles)``) never exceeds the configured
    #: ceiling for any tier: LIGHT 1 role * 0.25 = 0.25x, STANDARD 2
    #: roles * 0.375 = 0.75x, DEEP 2 roles * 0.5 = 1.0x (the full
    #: configured ceiling, exactly -- "DEEP may use the full configured
    #: budget").
    per_role_output_token_fraction: float
    escalated: bool = False
    escalation_reason: ReviewEffortReason | None = None


def _tier_semantics(
    tier: ReviewEffortTier, *, retry_ceiling: int
) -> tuple[float, bool, CriticExpectation, int, float]:
    """Returns ``(context_token_fraction, context_adaptive_enabled,
    critic_expectation, retry_limit, per_role_output_token_fraction)``
    for a tier -- the one place these engine-policy constants live (see
    spec section 26: repository config should not need to carry dozens
    of new knobs for this)."""

    if tier is ReviewEffortTier.LIGHT:
        return 0.5, False, CriticExpectation.OPTIONAL, min(1, retry_ceiling), 0.25
    if tier is ReviewEffortTier.STANDARD:
        return 1.0, True, CriticExpectation.SELECTIVE, retry_ceiling, 0.375
    return 1.0, True, CriticExpectation.MANDATORY, retry_ceiling, 0.5


class ReviewEffortPolicy:
    """Deterministic, side-effect-free. Composes
    :class:`~patchfrog.review.agents.selection.AgentSelectionPolicy`
    rather than duplicating its security-relevance logic."""

    def __init__(self, *, agent_selection_policy: AgentSelectionPolicy | None = None) -> None:
        self._agent_selection = agent_selection_policy or AgentSelectionPolicy()

    def decide_provisional(
        self,
        candidate: ReviewCandidate,
        *,
        static_findings: tuple[StaticFindingSummary, ...],
        max_retries: int,
    ) -> ReviewEffortDecision:
        agent_decisions = self._agent_selection.select(candidate, static_findings=static_findings)
        security_decision = next((d for d in agent_decisions if d.role is AgentRole.SECURITY), None)
        security_is_real_signal = (
            security_decision is not None
            and security_decision.reason != AgentSelectionReason.CONSERVATIVE_FALLBACK
        )

        high_severity_static = any(
            f.severity in (Severity.HIGH, Severity.CRITICAL) for f in static_findings
        )
        high_risk_category_static = any(f.category in _HIGH_RISK_STATIC_CATEGORIES for f in static_findings)
        span_lines = candidate.end_line - candidate.start_line
        is_large_symbol = span_lines > _LARGE_CHANGED_SYMBOL_LINES
        is_many_changed_lines = len(candidate.changed_lines) > _MANY_CHANGED_LINES

        reasons: list[ReviewEffortReason] = []
        signal_count = 0
        if static_findings:
            reasons.append(ReviewEffortReason.STATIC_FINDING_PRESENT)
            signal_count += 1
        if high_severity_static:
            reasons.append(ReviewEffortReason.STATIC_HIGH_SEVERITY)
            signal_count += 1
        if security_is_real_signal:
            reasons.append(ReviewEffortReason.SECURITY_RELEVANT)
            signal_count += 1
        if high_risk_category_static:
            reasons.append(ReviewEffortReason.HIGH_RISK_STATIC_CATEGORY)
            signal_count += 1
        if is_large_symbol:
            reasons.append(ReviewEffortReason.LARGE_CHANGED_SYMBOL)
            signal_count += 1
        if is_many_changed_lines:
            reasons.append(ReviewEffortReason.MANY_CHANGED_LINES)
            signal_count += 1

        if security_is_real_signal or high_severity_static or high_risk_category_static:
            tier = ReviewEffortTier.DEEP
        elif signal_count >= _MULTIPLE_SIGNALS_THRESHOLD:
            tier = ReviewEffortTier.DEEP
            reasons.append(ReviewEffortReason.MULTIPLE_STRUCTURAL_SIGNALS)
        elif signal_count >= 1:
            tier = ReviewEffortTier.STANDARD
        else:
            tier = ReviewEffortTier.LIGHT
            reasons = [ReviewEffortReason.NO_SIGNAL]

        selected_roles = frozenset(d.role for d in agent_decisions)
        if tier is ReviewEffortTier.LIGHT and not security_is_real_signal:
            # Never suppress Security when a real (non-fallback) signal
            # exists -- spec section 5: "Do not let LIGHT suppress a
            # clearly security-relevant candidate." A real signal always
            # forces DEEP above anyway, so this branch only ever drops
            # Security when its selection was the conservative fallback.
            selected_roles = selected_roles - {AgentRole.SECURITY}

        context_fraction, adaptive_enabled, critic_expectation, retry_limit, output_fraction = _tier_semantics(
            tier, retry_ceiling=max_retries
        )

        return ReviewEffortDecision(
            tier=tier,
            reasons=tuple(reasons),
            selected_roles=selected_roles,
            context_token_fraction=context_fraction,
            context_adaptive_enabled=adaptive_enabled,
            critic_expectation=critic_expectation,
            retry_limit=retry_limit,
            per_role_output_token_fraction=output_fraction,
        )

    def finalize(
        self, provisional: ReviewEffortDecision, *, adaptive_expansion_occurred: bool, max_retries: int
    ) -> ReviewEffortDecision:
        """Called once, after context is built, before any specialist
        provider call. The *only* escalation path in v1: bounded to
        exactly one step, never re-invoked, never triggered by anything
        but concrete adaptive-expansion evidence -- see module
        docstring."""

        if not adaptive_expansion_occurred or provisional.tier is ReviewEffortTier.DEEP:
            return provisional

        # Adaptive context is only ever built with adaptive mode enabled
        # for STANDARD/DEEP (LIGHT's context policy disables it -- see
        # _tier_semantics), so the only tier that can actually reach
        # here with real evidence is STANDARD. Escalation always targets
        # DEEP -- the tier that makes critic verification mandatory --
        # since that is the concrete safety consequence real depth-2
        # evidence should trigger.
        next_tier = ReviewEffortTier.DEEP
        context_fraction, adaptive_enabled, critic_expectation, retry_limit, output_fraction = _tier_semantics(
            next_tier, retry_ceiling=max_retries
        )
        return replace(
            provisional,
            tier=next_tier,
            reasons=(*provisional.reasons, ReviewEffortReason.ADAPTIVE_EXPANSION_OCCURRED),
            context_token_fraction=context_fraction,
            context_adaptive_enabled=adaptive_enabled,
            critic_expectation=critic_expectation,
            retry_limit=retry_limit,
            per_role_output_token_fraction=output_fraction,
            escalated=True,
            escalation_reason=ReviewEffortReason.ADAPTIVE_EXPANSION_OCCURRED,
        )

    def escalate_for_high_risk_proposal(
        self, decision: ReviewEffortDecision, *, high_risk_proposal_detected: bool, max_retries: int
    ) -> ReviewEffortDecision:
        """Called once per candidate, after specialist proposals are
        validated and cross-role-grouped, before critic verification --
        the *only* remaining escalation path a LIGHT candidate can
        actually reach (LIGHT disables adaptive context, so
        :meth:`finalize`'s stage-2 escalation path is structurally
        unreachable for it).

        ``high_risk_proposal_detected`` is computed by the caller
        (:class:`~patchfrog.review.orchestration.AgentOrchestrator`,
        which has the validated proposals and cross-role contradiction
        grouping in scope -- this module deliberately stays decoupled
        from proposal-level types) from a fixed, deterministic rule: a
        surviving (valid, not-yet-suppressed) proposal has HIGH/CRITICAL
        severity, security category, or is a member of an unresolved
        cross-role contradiction group.

        Never reruns a specialist role and never rebuilds context --
        both already happened; this only strengthens verification
        (critic becomes mandatory, and the critic's own retry ceiling
        rises to what DEEP would already allow) for whatever proposals
        already exist. Bounded to exactly one escalation, like
        :meth:`finalize` -- guarding on ``tier is DEEP`` catches a
        candidate that started DEEP, escalated at :meth:`finalize`, or
        already escalated here.
        """

        if not high_risk_proposal_detected or decision.tier is ReviewEffortTier.DEEP:
            return decision

        next_tier = ReviewEffortTier.DEEP
        context_fraction, adaptive_enabled, critic_expectation, retry_limit, output_fraction = _tier_semantics(
            next_tier, retry_ceiling=max_retries
        )
        return replace(
            decision,
            tier=next_tier,
            reasons=(*decision.reasons, ReviewEffortReason.HIGH_RISK_PROPOSAL),
            # Context/role selection are moot at this point (both already
            # happened) -- context_fraction/adaptive_enabled are carried
            # forward for a consistent, auditable record only, never
            # acted on again.
            context_token_fraction=context_fraction,
            context_adaptive_enabled=adaptive_enabled,
            critic_expectation=critic_expectation,
            retry_limit=retry_limit,
            per_role_output_token_fraction=output_fraction,
            escalated=True,
            escalation_reason=ReviewEffortReason.HIGH_RISK_PROPOSAL,
        )


def uniform_baseline_decision(*, max_retries: int) -> ReviewEffortDecision:
    """A fixed, tier-free decision resembling the pre-Quality-Cost-Guard
    behavior every candidate used to get: both specialist roles always
    selected (the pre-Milestone-F :class:`~patchfrog.review.agents.selection.AgentSelectionPolicy`
    default -- never LIGHT's tier-based role reduction), the existing
    adaptive context default on, the existing selective critic policy
    (unrelaxed, non-mandatory), and the full configured retry ceiling --
    exactly :func:`_tier_semantics`'s own STANDARD values, since "uniform
    baseline" is precisely "force STANDARD for every candidate, always
    both roles, never escalate." Reuses STANDARD's semantics rather than
    duplicating them so the deliberate output-ceiling-doubling *fix*
    this milestone makes (see :mod:`patchfrog.review.orchestration`'s
    module docstring) is never accidentally reintroduced into the
    comparison baseline -- that fix is a correctness fix, not a tiering
    feature, and applies identically in both ablation arms.

    Used exclusively for the evaluation harness's "uniform baseline"
    ablation (spec sections 24/25) -- comparing "current/uniform effort"
    against "quality-cost guard" runs without duplicating any production
    tiering logic. Never used by the real review path
    (:mod:`patchfrog.review.service`), which always computes a real
    :class:`ReviewEffortPolicy` decision.
    """

    context_fraction, adaptive_enabled, critic_expectation, retry_limit, output_fraction = _tier_semantics(
        ReviewEffortTier.STANDARD, retry_ceiling=max_retries
    )
    return ReviewEffortDecision(
        tier=ReviewEffortTier.STANDARD,
        reasons=(),
        selected_roles=frozenset({AgentRole.CORRECTNESS, AgentRole.SECURITY}),
        context_token_fraction=context_fraction,
        context_adaptive_enabled=adaptive_enabled,
        critic_expectation=critic_expectation,
        retry_limit=retry_limit,
        per_role_output_token_fraction=output_fraction,
    )
