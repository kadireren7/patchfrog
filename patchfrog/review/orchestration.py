"""Cooperative Agent Orchestration v1 -- the per-candidate entry point.

Wires together, for one candidate and its single, already-built
:class:`~patchfrog.review.agents.evidence.CandidateEvidencePackage`:

    deterministic role selection (AgentSelectionPolicy)
        -> concurrent, budget-guarded specialist calls (Correctness, Security)
        -> independent deterministic validation per proposal
        -> cross-role duplicate merge / contradiction detection
        -> selective critic verification (CriticSelectionPolicy)
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
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass, field

import structlog

from patchfrog.analysis.domain import Confidence
from patchfrog.context.tokens import estimate_tokens
from patchfrog.review.agents.cross_role import (
    group_cross_role,
    resolve_unresolved_contradictions,
)
from patchfrog.review.agents.evidence import CandidateEvidencePackage
from patchfrog.review.agents.proposal import AgentProposal
from patchfrog.review.agents.roles import AgentRole
from patchfrog.review.agents.selection import AgentSelectionPolicy
from patchfrog.review.critic import CriticService
from patchfrog.review.critic_selection import CriticSelectionInput, CriticSelectionPolicy
from patchfrog.review.domain import CriticVerdict, TokenUsage, ValidationOutcome
from patchfrog.review.prompt import build_agent_prompt
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


@dataclass(slots=True)
class CandidateOrchestrationResult:
    """Everything :mod:`patchfrog.review.service` needs to persist and
    aggregate one candidate's orchestrated review."""

    proposals: tuple[AgentProposal, ...] = ()
    reviewer_usage: TokenUsage = field(default_factory=TokenUsage)
    critic_usage: TokenUsage = field(default_factory=TokenUsage)
    usage_by_role: dict[AgentRole, TokenUsage] = field(default_factory=dict)
    #: One entry per role actually *called* (attempted), regardless of
    #: success/failure -- "for each candidate: at most one correctness
    #: call, at most one security call" (spec section 11), always 0 or 1
    #: per role since v1 never re-calls a role within one candidate.
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
        agent_selection_policy: AgentSelectionPolicy | None = None,
        critic_selection_policy: CriticSelectionPolicy | None = None,
    ) -> None:
        self._reviewer_providers = reviewer_providers
        self._critic = critic
        self._critic_enabled = critic_enabled
        self._max_output_tokens_per_candidate = max_output_tokens_per_candidate
        self._max_retries = max_retries
        self._agent_selection_policy = agent_selection_policy or AgentSelectionPolicy()
        self._critic_selection_policy = critic_selection_policy or CriticSelectionPolicy()

    async def review_candidate(
        self,
        evidence: CandidateEvidencePackage,
        *,
        min_final_confidence: Confidence,
        max_total_input_tokens: int,
        budget_lock: asyncio.Lock,
        budget_state: dict[str, int],
        log: structlog.stdlib.BoundLogger,
    ) -> CandidateOrchestrationResult:
        decisions = self._agent_selection_policy.select(
            evidence.candidate, static_findings=evidence.static_findings
        )
        selected_roles = tuple(d.role for d in decisions)

        prompts = {
            role: build_agent_prompt(
                role,
                candidate=evidence.candidate,
                context_text=evidence.context_text,
                diff_excerpt=evidence.diff_excerpt,
                static_findings=evidence.static_findings,
            )
            for role in selected_roles
        }
        combined_estimate = sum(
            estimate_tokens(system) + estimate_tokens(user) for system, user in prompts.values()
        )

        async with budget_lock:
            if budget_state["used_input_tokens"] + combined_estimate > max_total_input_tokens:
                return CandidateOrchestrationResult(skipped_budget=True)
            budget_state["used_input_tokens"] += combined_estimate

        results = await asyncio.gather(
            *(self._call_role(role, prompts[role]) for role in selected_roles),
            return_exceptions=True,
        )

        usage_by_role: dict[AgentRole, TokenUsage] = {}
        failed_roles: list[AgentRole] = []
        proposals: list[AgentProposal] = []
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

            raw_json, usage = outcome
            usage_by_role[role] = usage
            try:
                validated = parse_and_validate_response(raw_json, context=validation_context)
            except ResponseSchemaError as exc:
                failed_roles.append(role)
                log.warning("agent_role_response_schema_error", role=role.value, error=str(exc))
                continue

            for v in validated:
                proposals.append(AgentProposal(role=role, validated=v, reviewer_usage=usage))

        calls_by_role = dict.fromkeys(selected_roles, 1)

        if selected_roles and len(failed_roles) == len(selected_roles):
            return CandidateOrchestrationResult(
                failed=True,
                error=f"all selected agent roles failed: {[r.value for r in failed_roles]}",
                failed_roles=tuple(failed_roles),
                calls_by_role=calls_by_role,
            )

        proposals_t = tuple(proposals)
        grouping = group_cross_role(proposals_t)
        proposals_t = grouping.proposals

        proposals_t = await self._critique(
            proposals_t,
            candidate_evidence=evidence,
            contradiction_indices=grouping.contradiction_indices,
            min_final_confidence=min_final_confidence,
        )
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
        )

    async def _call_role(self, role: AgentRole, prompt: tuple[str, str]) -> tuple[str, TokenUsage]:
        system_prompt, user_prompt = prompt
        provider = self._reviewer_providers[role]
        request = ProviderRequest(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            json_schema=REVIEW_RESPONSE_SCHEMA,
            schema_name=f"review_response:{role.value}",
            max_output_tokens=self._max_output_tokens_per_candidate,
        )
        result = await call_with_retry(
            lambda: provider.generate_structured(request), max_retries=self._max_retries
        )
        return result.raw_json, TokenUsage(
            input_tokens=result.usage.input_tokens, output_tokens=result.usage.output_tokens
        )

    async def _critique(
        self,
        proposals: tuple[AgentProposal, ...],
        *,
        candidate_evidence: CandidateEvidencePackage,
        contradiction_indices: frozenset[int],
        min_final_confidence: Confidence,
    ) -> tuple[AgentProposal, ...]:
        if self._critic is None or not self._critic_enabled:
            return proposals

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

        to_critique: list[int] = []
        for i in valid_indices:
            if i in contradiction_indices:
                to_critique.append(i)
                continue
            peers = [selection_inputs[j] for j in valid_indices if j != i]
            if self._critic_selection_policy.should_critique(
                selection_inputs[i], peers=peers, min_final_confidence=min_final_confidence
            ):
                to_critique.append(i)

        if not to_critique:
            return proposals

        result = list(proposals)
        verdicts = await asyncio.gather(
            *(
                self._critique_one(
                    proposals[i],
                    candidate_evidence=candidate_evidence,
                    contradiction_indices=contradiction_indices,
                    all_proposals=proposals,
                )
                for i in to_critique
            ),
            return_exceptions=True,
        )
        for i, verdict_outcome in zip(to_critique, verdicts, strict=True):
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
            verdict, usage = verdict_outcome
            result[i] = result[i].with_critic_verdict(verdict, usage=usage)

        return tuple(result)

    async def _critique_one(
        self,
        proposal: AgentProposal,
        *,
        candidate_evidence: CandidateEvidencePackage,
        contradiction_indices: frozenset[int],
        all_proposals: tuple[AgentProposal, ...],
    ) -> tuple[CriticVerdict, TokenUsage]:
        critic = self._critic
        assert critic is not None
        conflicting = None
        if contradiction_indices:
            for other in all_proposals:
                if other is proposal or other.role == proposal.role:
                    continue
                if other.validated.finding.file_path == proposal.validated.finding.file_path:
                    conflicting = other.validated.finding
                    break

        verdict = await call_with_retry(
            lambda: critic.critique(
                proposal.validated,
                candidate=candidate_evidence.candidate,
                context_text=candidate_evidence.context_text,
                conflicting_finding=conflicting,
            ),
            max_retries=self._max_retries,
        )
        return verdict, TokenUsage(input_tokens=verdict.input_tokens, output_tokens=verdict.output_tokens)
