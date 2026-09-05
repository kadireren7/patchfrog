"""Deterministic telemetry collection over already-persisted review
state.

:func:`collect_review_telemetry` is the one canonical entry point (spec
section 20): given a ``review_run_id``, it queries existing persisted
data (never an LLM, never a network call) and returns a typed
:class:`~patchfrog.telemetry.domain.ReviewTelemetrySnapshot`. Same DB
state in, same snapshot out -- collection never mutates review state,
and a collection failure must never affect a review that already
completed (spec sections 20/42); callers that need that guarantee should
wrap this call themselves (see ``docs/telemetry-intelligence.md``'s
failure-semantics section) since this module deliberately stays
synchronous-with-the-DB and does not swallow its own errors.

Every multi-row lookup here issues a small, fixed number of queries
scoped to ``review_run_id`` (candidates, proposals, findings, feedback
events -- each one query, `.in_()`-batched where a second table is
joined by id list) -- never one query per candidate/proposal (spec
section 43: avoid N+1). See
``tests/unit/telemetry/test_telemetry_collector_query_bound.py``.
"""

from __future__ import annotations

import json
import uuid
from collections import defaultdict
from typing import cast

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from patchfrog.context.domain import ExpansionDirection, ExpansionReason
from patchfrog.feedback.assessment import compute_finding_assessment
from patchfrog.feedback.domain import FeedbackEvent
from patchfrog.feedback.queries import get_feedback_for_review
from patchfrog.persistence.models.context import ContextBundleModel
from patchfrog.persistence.models.review import (
    AIFindingModel,
    AIFindingProposalModel,
    ReviewCandidateModel,
)
from patchfrog.persistence.repositories import (
    AIFindingProposalRepository,
    AIFindingRepository,
    CriticVerdictRepository,
    ReviewCandidateRepository,
    ReviewRunRepository,
)
from patchfrog.review.agents.roles import AgentRole
from patchfrog.review.domain import CriticDecision, ProposalStatus, ReviewCandidateReason
from patchfrog.review.effort_types import ReviewEffortReason, ReviewEffortTier
from patchfrog.telemetry.domain import (
    TELEMETRY_SCHEMA_VERSION,
    CandidateTelemetry,
    ChangeIntelligenceTelemetry,
    ContextTelemetry,
    ContractIntelligenceTelemetry,
    FeedbackScope,
    FeedbackTelemetry,
    FindingLifecycleTelemetry,
    HistoricalRegressionMemoryTelemetry,
    IntentVerificationTelemetry,
    ProviderRoleUsage,
    ProviderTelemetry,
    ReviewFeedbackEventTelemetry,
    ReviewTelemetrySnapshot,
    TestIntelligenceTelemetry,
    classify_lifecycle_outcome,
)


def _load_json_list(raw: str) -> list[str]:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return []
    return value if isinstance(value, list) else []


def _load_json_dict(raw: str) -> dict[str, int]:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


async def _collect_context_telemetry(
    session: AsyncSession, *, candidates: list[ReviewCandidateModel]
) -> tuple[ContextTelemetry, ...]:
    bundle_id_to_candidate_id: dict[uuid.UUID, uuid.UUID] = {
        c.context_bundle_id: c.id for c in candidates if c.context_bundle_id is not None
    }
    if not bundle_id_to_candidate_id:
        return ()

    result = await session.execute(
        select(ContextBundleModel).where(ContextBundleModel.id.in_(bundle_id_to_candidate_id.keys()))
    )
    bundles = list(result.scalars().all())

    out: list[ContextTelemetry] = []
    for bundle in bundles:
        reasons = tuple(ExpansionReason(v) for v in _load_json_list(bundle.adaptive_expansion_reasons))
        direction: ExpansionDirection | None = (
            cast("ExpansionDirection", bundle.adaptive_expansion_direction)
            if bundle.adaptive_expansion_direction in ("callers", "callees", "both")
            else None
        )
        out.append(
            ContextTelemetry(
                bundle_id=bundle.id,
                candidate_id=bundle_id_to_candidate_id.get(bundle.id),
                engine_version=bundle.engine_version,
                total_tokens=bundle.total_tokens,
                total_lines=bundle.total_lines,
                generation_ms=bundle.generation_ms,
                adaptive_attempted=bundle.adaptive_expansion_attempted,
                adaptive_occurred=bundle.adaptive_expansion_occurred,
                adaptive_reasons=reasons,
                adaptive_direction=direction,
                depth_2_candidate_count=bundle.depth_2_candidate_count,
                depth_2_selected_count=bundle.depth_2_selected_count,
                depth_2_tokens=bundle.depth_2_tokens,
            )
        )
    return tuple(out)


async def _collect_feedback_telemetry(
    session: AsyncSession, *, review_run_id: uuid.UUID, findings: list[AIFindingModel]
) -> tuple[tuple[FeedbackTelemetry, ...], tuple[ReviewFeedbackEventTelemetry, ...]]:
    """Returns ``(finding_feedback, review_feedback)``. An event whose
    ``finding_id`` is ``None`` (best-effort attribution failed -- see
    :mod:`patchfrog.feedback.attribution`) is never dropped and never
    forced onto a finding it was never confirmed to be about; it is
    preserved instead as a :class:`ReviewFeedbackEventTelemetry` (spec
    sections 33/34)."""

    events = await get_feedback_for_review(session, review_run_id=review_run_id)
    by_finding: dict[uuid.UUID, list[FeedbackEvent]] = defaultdict(list)
    review_scoped: list[FeedbackEvent] = []
    for event in events:
        if event.finding_id is not None:
            by_finding[event.finding_id].append(event)
        else:
            review_scoped.append(event)

    review_feedback = tuple(
        ReviewFeedbackEventTelemetry(
            scope=FeedbackScope.REVIEW,
            event_type=event.event_type,
            source=event.source,
            normalized_signal=event.normalized_signal,
            signal_strength=event.signal_strength,
            occurred_at=event.occurred_at.isoformat(),
        )
        for event in review_scoped
    )

    out: list[FeedbackTelemetry] = []
    for finding in findings:
        finding_events = by_finding.get(finding.id, [])
        if not finding_events:
            out.append(
                FeedbackTelemetry(
                    finding_id=finding.id,
                    has_feedback=False,
                    usefulness_signal=None,
                    resolution_signal=None,
                    explicit_useful=0,
                    explicit_false_positive=0,
                    explicit_fixed=0,
                    explicit_ignore=0,
                    positive_reactions=0,
                    negative_reactions=0,
                )
            )
            continue
        summary = compute_finding_assessment(finding.id, finding_events)
        out.append(
            FeedbackTelemetry(
                finding_id=finding.id,
                has_feedback=True,
                usefulness_signal=summary.assessment.usefulness_signal,
                resolution_signal=summary.assessment.resolution_signal,
                explicit_useful=summary.explicit_useful,
                explicit_false_positive=summary.explicit_false_positive,
                explicit_fixed=summary.explicit_fixed,
                explicit_ignore=summary.explicit_ignore,
                positive_reactions=summary.positive_reactions,
                negative_reactions=summary.negative_reactions,
            )
        )
    return tuple(out), review_feedback


def _finding_lifecycle_from_proposal(
    proposal: AIFindingProposalModel,
    *,
    critic_decision: CriticDecision | None,
    finding_id: uuid.UUID | None,
    effort_tier: ReviewEffortTier | None,
) -> FindingLifecycleTelemetry:
    outcome = classify_lifecycle_outcome(status=proposal.status, critic_decision=critic_decision)
    return FindingLifecycleTelemetry(
        proposal_id=proposal.id,
        candidate_id=proposal.candidate_id,
        finding_id=finding_id,
        agent_role=proposal.agent_role,
        category=proposal.category,
        severity=proposal.severity,
        confidence=proposal.confidence,
        file_path=proposal.file_path,
        start_line=proposal.start_line,
        end_line=proposal.end_line,
        validation_outcome=proposal.validation_outcome,
        status=proposal.status,
        critic_decision=critic_decision,
        outcome=outcome,
        effort_tier=effort_tier,
    )


async def collect_review_telemetry(
    session: AsyncSession, *, review_run_id: uuid.UUID
) -> ReviewTelemetrySnapshot | None:
    """Collect the complete telemetry snapshot for one review run.
    Returns ``None`` if no run with this id exists -- never raises for a
    plain not-found (a caller/CLI decides how to report that)."""

    run = await ReviewRunRepository().get_by_id(session, run_id=review_run_id)
    if run is None:
        return None

    candidates = await ReviewCandidateRepository().list_for_run(session, review_run_id=review_run_id)
    proposals = await AIFindingProposalRepository().list_for_run(session, review_run_id=review_run_id)
    findings = await AIFindingRepository().list_for_run(session, review_run_id=review_run_id)
    verdicts = await CriticVerdictRepository().list_for_proposal_ids(
        session, proposal_ids=[p.id for p in proposals]
    )

    verdict_by_proposal = {v.proposal_id: v for v in verdicts}
    finding_id_by_proposal = {f.proposal_id: f.id for f in findings}

    proposals_by_candidate: dict[uuid.UUID, list[AIFindingProposalModel]] = defaultdict(list)
    for p in proposals:
        proposals_by_candidate[p.candidate_id].append(p)

    candidate_by_id = {c.id: c for c in candidates}

    finding_lifecycle = tuple(
        _finding_lifecycle_from_proposal(
            p,
            critic_decision=verdict_by_proposal[p.id].decision if p.id in verdict_by_proposal else None,
            finding_id=finding_id_by_proposal.get(p.id),
            effort_tier=candidate_by_id[p.candidate_id].effort_tier if p.candidate_id in candidate_by_id else None,
        )
        for p in proposals
    )

    candidate_telemetry = tuple(
        CandidateTelemetry(
            candidate_id=c.id,
            file_path=c.file_path,
            reason=ReviewCandidateReason(c.reason),
            status=c.status.value,
            effort_tier=c.effort_tier,
            effort_reasons=tuple(ReviewEffortReason(v) for v in _load_json_list(c.effort_reasons)),
            escalated=c.escalated,
            escalation_reason=c.escalation_reason,
            context_bundle_id=c.context_bundle_id,
            static_finding_count=len(_load_json_list(c.static_finding_ids)),
            proposals_count=len(proposals_by_candidate.get(c.id, [])),
            accepted_count=sum(
                1 for p in proposals_by_candidate.get(c.id, []) if p.status is ProposalStatus.ACCEPTED
            ),
        )
        for c in candidates
    )

    calls_by_role_raw = _load_json_dict(run.calls_by_role)
    reviewer_by_role = tuple(
        ProviderRoleUsage(
            role=role,
            calls=calls_by_role_raw.get(role.value, 0),
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            thinking_tokens=thinking_tokens,
        )
        for role, input_tokens, output_tokens, thinking_tokens in (
            (AgentRole.CORRECTNESS, run.correctness_input_tokens, run.correctness_output_tokens, run.correctness_thinking_tokens),
            (AgentRole.SECURITY, run.security_input_tokens, run.security_output_tokens, run.security_thinking_tokens),
        )
    )
    provider = ProviderTelemetry(
        reviewer_provider=run.reviewer_provider,
        reviewer_model=run.reviewer_model,
        critic_provider=run.critic_provider,
        critic_model=run.critic_model,
        reviewer_calls_total=sum(calls_by_role_raw.values()),
        reviewer_input_tokens_total=run.reviewer_input_tokens,
        reviewer_output_tokens_total=run.reviewer_output_tokens,
        reviewer_thinking_tokens_total=run.reviewer_thinking_tokens,
        reviewer_by_role=reviewer_by_role,
        reviewer_latency_ms_aggregate=run.reviewer_latency_ms,
        critic_calls_total=run.critic_calls,
        critic_input_tokens_total=run.critic_input_tokens,
        critic_output_tokens_total=run.critic_output_tokens,
        critic_thinking_tokens_total=run.critic_thinking_tokens,
        critic_latency_ms_aggregate=sum(v.latency_ms for v in verdicts),
        retries_consumed=run.retries_consumed,
    )

    context_telemetry = await _collect_context_telemetry(session, candidates=candidates)
    feedback_telemetry, review_feedback_telemetry = await _collect_feedback_telemetry(
        session, review_run_id=review_run_id, findings=findings
    )

    change_kind_counts_raw = _load_json_dict(run.change_kind_counts)
    change_intelligence = ChangeIntelligenceTelemetry(
        change_unit_count=run.change_unit_count,
        change_kind_counts=tuple(sorted(change_kind_counts_raw.items())),
        affected_surface_count=run.affected_surface_count,
        expected_companion_count=run.expected_companion_count,
        missing_companion_candidate_count=run.missing_companion_candidate_count,
        change_map_rendered=run.change_map_rendered,
        change_map_node_count=run.change_map_node_count,
    )

    contract_kind_counts_raw = _load_json_dict(run.contract_kind_counts)
    contract_intelligence = ContractIntelligenceTelemetry(
        contract_delta_count=run.contract_delta_count,
        contract_kind_counts=tuple(sorted(contract_kind_counts_raw.items())),
        potentially_breaking_delta_count=run.potentially_breaking_delta_count,
        impacted_consumer_count=run.impacted_consumer_count,
        stale_consumer_candidate_count=run.stale_consumer_candidate_count,
    )

    intent_source_kind_counts_raw = _load_json_dict(run.intent_source_kind_counts)
    intent_verification = IntentVerificationTelemetry(
        intent_evidence_available=run.intent_claim_count > 0,
        intent_claim_count=run.intent_claim_count,
        intent_source_kind_counts=tuple(sorted(intent_source_kind_counts_raw.items())),
        mapped_intent_claim_count=run.mapped_intent_claim_count,
        intent_gap_candidate_count=run.intent_gap_candidate_count,
        intent_coverage_summary_rendered=run.intent_coverage_summary_rendered,
    )

    test_reason_code_counts_raw = _load_json_dict(run.test_reason_code_counts)
    test_intelligence = TestIntelligenceTelemetry(
        test_expectation_count=run.test_expectation_count,
        test_reason_code_counts=tuple(sorted(test_reason_code_counts_raw.items())),
        test_gap_candidate_count=run.test_gap_candidate_count,
        test_coverage_summary_rendered=run.test_coverage_summary_rendered,
    )

    historical_match_kind_counts_raw = _load_json_dict(run.historical_match_kind_counts)
    historical_regression_memory = HistoricalRegressionMemoryTelemetry(
        historical_trusted_record_count=run.historical_trusted_record_count,
        historical_match_kind_counts=tuple(sorted(historical_match_kind_counts_raw.items())),
        historical_regression_candidate_count=run.historical_regression_candidate_count,
        historical_summary_rendered=run.historical_summary_rendered,
    )

    return ReviewTelemetrySnapshot(
        schema_version=TELEMETRY_SCHEMA_VERSION,
        review_run_id=run.id,
        repository_id=run.repository_id,
        pull_request_id=run.pull_request_id,
        status=run.status.value,
        commit_sha=run.commit_sha,
        started_at=run.started_at.isoformat(),
        completed_at=run.completed_at.isoformat() if run.completed_at is not None else None,
        duration_ms=run.duration_ms,
        candidate_count=run.candidate_count,
        candidates_reviewed=run.candidates_reviewed,
        candidates_failed=run.candidates_failed,
        candidates_skipped_budget=run.candidates_skipped_budget,
        candidates_escalated=run.candidates_escalated,
        candidates=candidate_telemetry,
        finding_lifecycle=finding_lifecycle,
        provider=provider,
        context=context_telemetry,
        feedback=feedback_telemetry,
        review_feedback=review_feedback_telemetry,
        change_intelligence=change_intelligence,
        contract_intelligence=contract_intelligence,
        intent_verification=intent_verification,
        test_intelligence=test_intelligence,
        historical_regression_memory=historical_regression_memory,
    )
