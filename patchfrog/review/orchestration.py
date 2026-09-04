"""Cooperative Agent Orchestration v1 -- the per-candidate entry point.

Wires together, for one candidate and its single, already-built
:class:`~patchfrog.review.agents.evidence.CandidateEvidencePackage`:

    Quality + Cost Guard tiering (patchfrog.review.effort, decided upstream
    by patchfrog.review.service, passed in as a ReviewEffortDecision)
        -> concurrent, budget-guarded specialist calls (Correctness, Security)
        -> independent deterministic validation per proposal
        -> cross-role duplicate merge / contradiction detection
        -> post-proposal escalation (a surviving proposal's own risk profile
           may raise the decision to DEEP -- see _detect_high_risk_proposal)
        -> tier-aware critic verification (CriticSelectionPolicy + CriticExpectation)
        -> contradiction resolution (suppress if the critic can't resolve it)

Replaces the pre-orchestration assumption of "one reviewer response per
candidate" inside :mod:`patchfrog.review.service`, while leaving that
module's run-level orchestration (candidate generation, persistence,
dedup at the FinalAIFinding level, confidence aggregation) untouched --
this module only changes what happens *inside* one candidate's review.

"The model may propose. PatchFrog decides what survives." -- every
proposal from either specialist still passes through the identical
deterministic validation gate (:mod:`patchfrog.review.validation`)
independently; no proposal is ever trusted because a sibling proposal
from the other role already passed.

Role selection is no longer decided here -- it comes from the
:class:`~patchfrog.review.effort.ReviewEffortDecision` the caller
(:mod:`patchfrog.review.service`) already computed via
:class:`~patchfrog.review.effort.ReviewEffortPolicy` *before* any
provider call. This module is purely an executor of that decision: it
never re-derives which roles should run, and it never controls
provider/model/credentials (:mod:`patchfrog.review.runtime_config`
remains the sole, operator-controlled authority for that).

``max_output_tokens_per_candidate`` is a shared, candidate-level output
ceiling (not a per-role ceiling) -- each selected role gets a
deterministic, tier-fixed fraction of it
(:attr:`~patchfrog.review.effort.ReviewEffortDecision.per_role_output_token_fraction`),
chosen so that even at DEEP (every role selected, the largest per-role
fraction) the roles' *combined* spend never exceeds the configured
ceiling -- two concurrently-run specialist roles can never together
spend more than it.

``max_total_input_tokens`` is a true run-level guard: both reviewer
(Correctness/Security) input *and* critic input are reserved against it,
atomically, before the corresponding provider call is made. Reservations
are estimates (see :func:`patchfrog.context.tokens.estimate_tokens`);
once a call actually completes, the reservation is reconciled against
the provider's *actual* reported usage under the same lock -- crediting
back an overestimate (including a failed call's now-unused reservation)
and debiting further for an underestimate, never letting the tracked
total go negative. A required critic verification that cannot be
reserved never publishes anyway -- see :data:`CRITIC_BUDGET_EXHAUSTED`.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass, field

import structlog

from patchfrog.analysis.domain import Confidence, FindingCategory, Severity
from patchfrog.context.tokens import estimate_tokens
from patchfrog.review.agents.cross_role import (
    group_cross_role,
    resolve_unresolved_contradictions,
)
from patchfrog.review.agents.evidence import CandidateEvidencePackage
from patchfrog.review.agents.proposal import AgentProposal
from patchfrog.review.agents.roles import AgentRole
from patchfrog.review.critic import CriticService
from patchfrog.review.critic_selection import CriticSelectionInput, CriticSelectionPolicy
from patchfrog.review.domain import AIReviewFinding, CriticVerdict, TokenUsage, ValidationOutcome
from patchfrog.review.effort import ReviewEffortDecision, ReviewEffortPolicy
from patchfrog.review.effort_types import CriticExpectation
from patchfrog.review.prompt import build_agent_prompt, build_critic_prompt
from patchfrog.review.provider import (
    LLMProvider,
    ProviderFatalError,
    ProviderRequest,
    ProviderTransientError,
)
from patchfrog.review.retry import call_with_retry
from patchfrog.review.schemas import REVIEW_RESPONSE_SCHEMA
from patchfrog.review.validation import (
    ResponseSchemaError,
    ValidationContext,
    parse_and_validate_response,
)

logger = structlog.get_logger(__name__)

#: Suppression reason recorded on
#: :attr:`~patchfrog.review.agents.proposal.AgentProposal.suppressed_reason`
#: when a proposal's required critic verification could not be reserved
#: against the run's remaining ``max_total_input_tokens``. Spec section
#: 21/15: a cost-saving decision must never let an unverified,
#: risk-flagged proposal publish -- suppressing is always the safe
#: outcome, never "publish because the reviewer call was already paid
#: for."
CRITIC_BUDGET_EXHAUSTED = "critic_budget_exhausted"


@dataclass(slots=True)
class CandidateOrchestrationResult:
    """Everything :mod:`patchfrog.review.service` needs to persist and
    aggregate one candidate's orchestrated review."""

    proposals: tuple[AgentProposal, ...] = ()
    reviewer_usage: TokenUsage = field(default_factory=TokenUsage)
    critic_usage: TokenUsage = field(default_factory=TokenUsage)
    usage_by_role: dict[AgentRole, TokenUsage] = field(default_factory=dict)
    #: One entry per role actually *called* (attempted), regardless of
    #: success/failure -- always 0 or 1 per role since v1 never re-calls
    #: a role within one candidate.
    calls_by_role: dict[AgentRole, int] = field(default_factory=dict)
    #: Set when the combined estimated input for every selected role's
    #: prompt would exceed the run's remaining ``max_total_input_tokens``
    #: -- the whole candidate is skipped, never just one role, so a
    #: partially-orchestrated candidate never silently happens.
    skipped_budget: bool = False
    #: Set only when *every* selected role's call failed -- a single
    #: specialist failing while another succeeds is not a candidate
    #: failure (see module docstring / spec section 20).
    failed: bool = False
    error: str | None = None
    failed_roles: tuple[AgentRole, ...] = ()
    #: Quality + Cost Guard accounting: critic calls actually made and
    #: total retry attempts actually consumed (reviewer + critic) for
    #: this one candidate -- see :mod:`patchfrog.review.effort`.
    critic_calls: int = 0
    retries_consumed: int = 0
    #: Sum of every specialist role call's :attr:`~patchfrog.review.provider.ProviderResult.latency_ms`
    #: for this candidate -- a *provider-work* latency aggregate, never a
    #: wall-clock measurement (Correctness/Security calls run
    #: concurrently via :func:`asyncio.gather`, so this can legitimately
    #: exceed the candidate's actual wall-clock time). Telemetry
    #: (:mod:`patchfrog.telemetry`) is the only consumer that needs this
    #: distinction spelled out explicitly -- see its module docstring.
    reviewer_latency_ms: float = 0.0
    #: The *effective* effort decision after this candidate's own
    #: post-proposal escalation check
    #: (:meth:`~patchfrog.review.effort.ReviewEffortPolicy.escalate_for_high_risk_proposal`)
    #: -- ``None`` only on the ``skipped_budget``/``failed`` early-return
    #: paths, where no proposal ever existed to escalate on and the
    #: caller's own pre-call decision remains authoritative. The caller
    #: (:mod:`patchfrog.review.service`) persists *this* decision, not
    #: the one it passed in, so a candidate that escalated here is never
    #: persisted under its stale pre-escalation tier.
    effort_decision: ReviewEffortDecision | None = None


def _detect_high_risk_proposal(
    proposals: tuple[AgentProposal, ...], *, contradiction_indices: frozenset[int]
) -> bool:
    """The post-proposal escalation trigger (spec: bounded escalation
    from a validated proposal's own risk profile, the only escalation
    path a LIGHT candidate can actually reach -- see
    :mod:`patchfrog.review.effort`'s module docstring for why LIGHT can
    never reach :meth:`~patchfrog.review.effort.ReviewEffortPolicy.finalize`'s
    adaptive-expansion path instead).

    Deterministic, fixed rule -- checked against every proposal that
    passed validation and was not already suppressed by cross-role
    dedup, regardless of which *role* produced it: nothing in the
    response schema prevents a Correctness-role response from returning
    a security-categorized finding, so this never assumes
    ``proposal.role`` determines ``finding.category``.
    """

    for i, proposal in enumerate(proposals):
        if proposal.validated.outcome != ValidationOutcome.VALID or proposal.suppressed_reason is not None:
            continue
        if i in contradiction_indices:
            return True
        finding = proposal.validated.finding
        if finding.severity in (Severity.HIGH, Severity.CRITICAL):
            return True
        if finding.category is FindingCategory.SECURITY:
            return True
    return False


def _find_conflicting(
    proposal: AgentProposal, *, all_proposals: tuple[AgentProposal, ...], contradiction_indices: frozenset[int]
) -> AIReviewFinding | None:
    """Another specialist's proposal about the same file that the
    cross-role heuristic flagged as part of a contradiction group --
    shown to the critic as data to weigh, never as an instruction. Also
    used (before any critic call) to build the exact same prompt the
    critic will see, purely to estimate its token cost for budget
    reservation."""

    if not contradiction_indices:
        return None
    for other in all_proposals:
        if other is proposal or other.role == proposal.role:
            continue
        if other.validated.finding.file_path == proposal.validated.finding.file_path:
            return other.validated.finding
    return None


class AgentOrchestrator:
    """Provider-neutral by design: ``reviewer_providers`` maps each
    :class:`AgentRole` to the :class:`~patchfrog.review.provider.LLMProvider`
    that serves it. v1 always maps both roles to the same
    operator-configured provider (see
    :func:`patchfrog.review.provider_factory.build_reviewer_provider`) --
    different role *prompts*, not different providers/models. The
    mapping shape itself already supports future role-specific model
    routing without any repository-controlled field ever existing for
    it (see spec section 2)."""

    def __init__(
        self,
        *,
        reviewer_providers: Mapping[AgentRole, LLMProvider],
        critic: CriticService | None,
        critic_enabled: bool,
        max_output_tokens_per_candidate: int,
        max_retries: int,
        critic_selection_policy: CriticSelectionPolicy | None = None,
        effort_policy: ReviewEffortPolicy | None = None,
    ) -> None:
        self._reviewer_providers = reviewer_providers
        self._critic = critic
        self._critic_enabled = critic_enabled
        self._max_output_tokens_per_candidate = max_output_tokens_per_candidate
        self._max_retries = max_retries
        self._critic_selection_policy = critic_selection_policy or CriticSelectionPolicy()
        #: Used only for the post-proposal escalation check (spec:
        #: bounded escalation from a validated proposal's own risk
        #: profile) -- role/context/output-budget decisions still come
        #: entirely from the ``effort_decision`` the caller passes into
        #: :meth:`review_candidate`, never re-derived here.
        self._effort_policy = effort_policy or ReviewEffortPolicy()

    async def review_candidate(
        self,
        evidence: CandidateEvidencePackage,
        *,
        effort_decision: ReviewEffortDecision,
        min_final_confidence: Confidence,
        max_total_input_tokens: int,
        budget_lock: asyncio.Lock,
        budget_state: dict[str, int],
        log: structlog.stdlib.BoundLogger,
        allow_post_proposal_escalation: bool = True,
    ) -> CandidateOrchestrationResult:
        """``allow_post_proposal_escalation=False`` is the evaluation
        harness's "uniform baseline" ablation hook
        (:mod:`patchfrog.review.effort`'s ``uniform_baseline_decision``,
        threaded through :class:`~patchfrog.review.service.PullRequestReviewService`) --
        a genuinely *fixed* comparison baseline must never escalate,
        exactly like :meth:`~patchfrog.review.effort.ReviewEffortPolicy.finalize`
        is already skipped for it. Every real review leaves this at the
        default ``True``."""
        # Fixed canonical order, not set iteration order, so zip()/log
        # output/tests are deterministic regardless of frozenset hashing.
        selected_roles = tuple(
            r for r in (AgentRole.CORRECTNESS, AgentRole.SECURITY) if r in effort_decision.selected_roles
        )
        if not selected_roles:
            return CandidateOrchestrationResult()

        # Shared candidate-level output ceiling, allocated per selected
        # role via a tier-fixed per-role fraction (see
        # ReviewEffortDecision.per_role_output_token_fraction's docstring
        # for why this is NOT "candidate-level fraction / role count").
        # Never zero even at the smallest tier/role-count combination.
        role_max_output_tokens = max(
            1,
            int(self._max_output_tokens_per_candidate * effort_decision.per_role_output_token_fraction),
        )
        role_max_retries = min(self._max_retries, effort_decision.retry_limit)

        prompts = {
            role: build_agent_prompt(
                role,
                candidate=evidence.candidate,
                context_text=evidence.context_text,
                diff_excerpt=evidence.diff_excerpt,
                static_findings=evidence.static_findings,
                change_intelligence_text=evidence.change_intelligence_text,
                contract_intelligence_text=evidence.contract_intelligence_text,
                intent_verification_text=evidence.intent_verification_text,
            )
            for role in selected_roles
        }
        role_estimates = {
            role: estimate_tokens(system) + estimate_tokens(user) for role, (system, user) in prompts.items()
        }
        combined_estimate = sum(role_estimates.values())

        async with budget_lock:
            if budget_state["used_input_tokens"] + combined_estimate > max_total_input_tokens:
                log.warning("review_budget_exhausted", stage="reviewer", roles=[r.value for r in selected_roles])
                return CandidateOrchestrationResult(skipped_budget=True)
            budget_state["used_input_tokens"] += combined_estimate

        results = await asyncio.gather(
            *(
                self._call_role(
                    role, prompts[role], max_output_tokens=role_max_output_tokens, max_retries=role_max_retries
                )
                for role in selected_roles
            ),
            return_exceptions=True,
        )

        usage_by_role: dict[AgentRole, TokenUsage] = {}
        failed_roles: list[AgentRole] = []
        proposals: list[AgentProposal] = []
        retries_consumed = 0
        actual_input_total = 0
        reviewer_latency_ms = 0.0
        validation_context = ValidationContext(
            allowed_file_paths=evidence.allowed_file_paths,
            context_text=evidence.context_text,
            diff_excerpt=evidence.diff_excerpt,
        )

        for role, outcome in zip(selected_roles, results, strict=True):
            if isinstance(outcome, BaseException):
                if isinstance(outcome, (ProviderFatalError, ProviderTransientError)):
                    failed_roles.append(role)
                    log.warning("agent_role_call_failed", role=role.value, error=str(outcome))
                    continue
                raise outcome

            raw_json, usage, retries_used, latency_ms = outcome
            usage_by_role[role] = usage
            actual_input_total += usage.input_tokens
            retries_consumed += retries_used
            reviewer_latency_ms += latency_ms
            try:
                validated = parse_and_validate_response(raw_json, context=validation_context)
            except ResponseSchemaError as exc:
                failed_roles.append(role)
                log.warning("agent_role_response_schema_error", role=role.value, error=str(exc))
                continue

            for v in validated:
                proposals.append(AgentProposal(role=role, validated=v, reviewer_usage=usage))

        # Reconcile the reservation against ACTUAL usage (spec sections
        # 33/34): a failed/excepted role contributed 0 actual tokens and
        # its share of the estimate is credited back; an underestimated
        # prompt debits the run's remaining budget further. Never allowed
        # to go negative.
        async with budget_lock:
            budget_state["used_input_tokens"] = max(
                0, budget_state["used_input_tokens"] - combined_estimate + actual_input_total
            )

        calls_by_role = dict.fromkeys(selected_roles, 1)

        if selected_roles and len(failed_roles) == len(selected_roles):
            return CandidateOrchestrationResult(
                failed=True,
                error=f"all selected agent roles failed: {[r.value for r in failed_roles]}",
                failed_roles=tuple(failed_roles),
                calls_by_role=calls_by_role,
                retries_consumed=retries_consumed,
            )

        proposals_t = tuple(proposals)
        grouping = group_cross_role(proposals_t)
        proposals_t = grouping.proposals

        # Post-proposal escalation (spec: bounded escalation from a
        # validated proposal's own risk profile) -- the only escalation
        # path a LIGHT candidate can actually reach, since LIGHT disables
        # adaptive context and so can never trigger
        # ReviewEffortPolicy.finalize's stage-2 path. Never reruns a
        # specialist role and never rebuilds context (both already
        # happened above) -- this only strengthens critic verification
        # for whatever proposals already exist. At most one escalation
        # total (escalate_for_high_risk_proposal guards on tier is DEEP,
        # which also catches a candidate already escalated by finalize).
        high_risk_detected = allow_post_proposal_escalation and _detect_high_risk_proposal(
            proposals_t, contradiction_indices=grouping.contradiction_indices
        )
        effort_decision = self._effort_policy.escalate_for_high_risk_proposal(
            effort_decision, high_risk_proposal_detected=high_risk_detected, max_retries=self._max_retries
        )
        critic_max_retries = min(self._max_retries, effort_decision.retry_limit)

        proposals_t, critic_calls, critic_retries = await self._critique(
            proposals_t,
            candidate_evidence=evidence,
            contradiction_indices=grouping.contradiction_indices,
            min_final_confidence=min_final_confidence,
            critic_expectation=effort_decision.critic_expectation,
            max_retries=critic_max_retries,
            max_total_input_tokens=max_total_input_tokens,
            budget_lock=budget_lock,
            budget_state=budget_state,
            log=log,
        )
        retries_consumed += critic_retries
        proposals_t = resolve_unresolved_contradictions(
            proposals_t, contradiction_indices=grouping.contradiction_indices
        )

        # Charge reviewer usage once per role *call*, not once per finding --
        # a role that returns multiple findings in one response must not
        # inflate the run's reported token usage.
        reviewer_usage = sum(usage_by_role.values(), TokenUsage())
        critic_usage = sum((p.critic_usage for p in proposals_t), TokenUsage())

        return CandidateOrchestrationResult(
            proposals=proposals_t,
            reviewer_usage=reviewer_usage,
            critic_usage=critic_usage,
            usage_by_role=usage_by_role,
            failed_roles=tuple(failed_roles),
            calls_by_role=calls_by_role,
            critic_calls=critic_calls,
            retries_consumed=retries_consumed,
            effort_decision=effort_decision,
            reviewer_latency_ms=reviewer_latency_ms,
        )

    async def _call_role(
        self, role: AgentRole, prompt: tuple[str, str], *, max_output_tokens: int, max_retries: int
    ) -> tuple[str, TokenUsage, int, float]:
        system_prompt, user_prompt = prompt
        provider = self._reviewer_providers[role]
        request = ProviderRequest(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            json_schema=REVIEW_RESPONSE_SCHEMA,
            schema_name=f"review_response:{role.value}",
            max_output_tokens=max_output_tokens,
        )
        result, retries_used = await call_with_retry(
            lambda: provider.generate_structured(request), max_retries=max_retries
        )
        return (
            result.raw_json,
            TokenUsage(
                input_tokens=result.usage.input_tokens,
                output_tokens=result.usage.output_tokens,
                thinking_tokens=result.usage.thinking_tokens,
            ),
            retries_used,
            result.latency_ms,
        )

    async def _critique(
        self,
        proposals: tuple[AgentProposal, ...],
        *,
        candidate_evidence: CandidateEvidencePackage,
        contradiction_indices: frozenset[int],
        min_final_confidence: Confidence,
        critic_expectation: CriticExpectation,
        max_retries: int,
        max_total_input_tokens: int,
        budget_lock: asyncio.Lock,
        budget_state: dict[str, int],
        log: structlog.stdlib.BoundLogger,
    ) -> tuple[tuple[AgentProposal, ...], int, int]:
        """Returns ``(proposals, critic_calls, retries_consumed)``.

        ``critic_expectation`` (see :mod:`patchfrog.review.effort`)
        controls strictness without changing *what* the critic checks:
        ``MANDATORY`` bypasses :class:`CriticSelectionPolicy` entirely
        (every valid proposal is critiqued, same as an unresolved
        contradiction already forces today); ``OPTIONAL`` passes
        ``relaxed=True`` through to it; ``SELECTIVE`` is today's
        unchanged behavior.
        """

        if self._critic is None or not self._critic_enabled:
            return proposals, 0, 0

        valid_indices = [
            i for i, p in enumerate(proposals)
            if p.validated.outcome == ValidationOutcome.VALID and p.suppressed_reason is None
        ]
        selection_inputs = {
            i: CriticSelectionInput(
                role=proposals[i].role,
                category=proposals[i].validated.finding.category,
                severity=proposals[i].validated.finding.severity,
                confidence=proposals[i].validated.finding.confidence,
                corroborated_by_static=bool(candidate_evidence.candidate.static_finding_ids),
                file_path=proposals[i].validated.finding.file_path,
                start_line=proposals[i].validated.finding.start_line,
                end_line=proposals[i].validated.finding.end_line,
            )
            for i in valid_indices
        }

        relaxed = critic_expectation is CriticExpectation.OPTIONAL
        mandatory_all = critic_expectation is CriticExpectation.MANDATORY

        to_critique: list[int] = []
        for i in valid_indices:
            if i in contradiction_indices or mandatory_all:
                to_critique.append(i)
                continue
            peers = [selection_inputs[j] for j in valid_indices if j != i]
            if self._critic_selection_policy.should_critique(
                selection_inputs[i], peers=peers, min_final_confidence=min_final_confidence, relaxed=relaxed
            ):
                to_critique.append(i)

        if not to_critique:
            return proposals, 0, 0

        # Reserve each candidate-for-critique's estimated input cost
        # atomically, in order, *before* issuing any provider call --
        # spec section 15/21: a proposal whose required verification
        # can't be reserved is suppressed, never published unverified.
        # This is per-proposal (not "all or nothing" like the reviewer
        # reservation above) since each critique is an independent,
        # separately-billed provider call.
        result = list(proposals)
        reserved: list[int] = []
        reserved_estimates: dict[int, int] = {}
        for i in to_critique:
            proposal = proposals[i]
            conflicting = _find_conflicting(
                proposal, all_proposals=proposals, contradiction_indices=contradiction_indices
            )
            system_prompt, user_prompt = build_critic_prompt(
                candidate=candidate_evidence.candidate,
                context_text=candidate_evidence.context_text,
                finding=proposal.validated.finding,
                conflicting_finding=conflicting,
            )
            estimate = estimate_tokens(system_prompt) + estimate_tokens(user_prompt)
            async with budget_lock:
                if budget_state["used_input_tokens"] + estimate > max_total_input_tokens:
                    ok = False
                else:
                    budget_state["used_input_tokens"] += estimate
                    ok = True
            if not ok:
                result[i] = result[i].suppressed(CRITIC_BUDGET_EXHAUSTED)
                log.warning("review_budget_exhausted", stage="critic", role=proposal.role.value)
                continue
            reserved.append(i)
            reserved_estimates[i] = estimate

        if not reserved:
            return tuple(result), 0, 0

        verdicts = await asyncio.gather(
            *(
                self._critique_one(
                    proposals[i],
                    candidate_evidence=candidate_evidence,
                    contradiction_indices=contradiction_indices,
                    all_proposals=proposals,
                    max_retries=max_retries,
                )
                for i in reserved
            ),
            return_exceptions=True,
        )

        critic_calls = 0
        retries_consumed = 0
        actual_total = 0
        for i, verdict_outcome in zip(reserved, verdicts, strict=True):
            critic_calls += 1
            if isinstance(verdict_outcome, BaseException):
                if isinstance(verdict_outcome, (ProviderFatalError, ProviderTransientError, ResponseSchemaError)):
                    # Safe fallback -- no verdict, deterministic validation
                    # already ran. See module docstring / spec section 20.
                    logger.warning(
                        "agent_critic_failed",
                        role=proposals[i].role.value,
                        error=str(verdict_outcome),
                    )
                    continue
                raise verdict_outcome
            verdict, usage, retries_used = verdict_outcome
            retries_consumed += retries_used
            actual_total += usage.input_tokens
            result[i] = result[i].with_critic_verdict(verdict, usage=usage)

        async with budget_lock:
            total_estimate = sum(reserved_estimates[i] for i in reserved)
            budget_state["used_input_tokens"] = max(
                0, budget_state["used_input_tokens"] - total_estimate + actual_total
            )

        return tuple(result), critic_calls, retries_consumed

    async def _critique_one(
        self,
        proposal: AgentProposal,
        *,
        candidate_evidence: CandidateEvidencePackage,
        contradiction_indices: frozenset[int],
        all_proposals: tuple[AgentProposal, ...],
        max_retries: int,
    ) -> tuple[CriticVerdict, TokenUsage, int]:
        critic = self._critic
        assert critic is not None
        conflicting = _find_conflicting(
            proposal, all_proposals=all_proposals, contradiction_indices=contradiction_indices
        )

        verdict, retries_used = await call_with_retry(
            lambda: critic.critique(
                proposal.validated,
                candidate=candidate_evidence.candidate,
                context_text=candidate_evidence.context_text,
                conflicting_finding=conflicting,
            ),
            max_retries=max_retries,
        )
        return (
            verdict,
            TokenUsage(
                input_tokens=verdict.input_tokens,
                output_tokens=verdict.output_tokens,
                thinking_tokens=verdict.thinking_tokens,
            ),
            retries_used,
        )
