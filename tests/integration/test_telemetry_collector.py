"""Integration coverage for :func:`patchfrog.telemetry.collector.collect_review_telemetry`
against real persisted state (SQLite in-memory, via the shared
``session_factory`` fixture in ``tests/integration/conftest.py``).

Two strategies, deliberately combined:

- Most tests here build one exhaustive scenario by persisting rows
  directly through the same repositories production code uses (never
  hand-rolled ORM field guessing) -- this makes every
  :class:`~patchfrog.review.domain.ProposalStatus`/
  :class:`~patchfrog.review.domain.CriticDecision` combination, every
  historical-nullable-row case, and the redaction guarantee
  deterministic and fast to set up, instead of trying to coerce the full
  cooperative-orchestration pipeline into eight different outcomes
  organically.
- One additional test (``test_collector_against_a_real_review_pipeline_run``)
  runs the actual production pipeline (``PullRequestReviewService.review_local``
  via the shared ``ai_review_python`` fixture, see ``tests/support/publishing.py``)
  end to end and collects real telemetry from it -- a guard against the
  hand-rolled scenario drifting from what production actually produces.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from patchfrog.analysis.domain import Confidence, FindingCategory, Severity
from patchfrog.context.domain import ContextTargetType
from patchfrog.feedback.domain import (
    ActorIdentity,
    ExplicitCommand,
    FeedbackEvent,
    FeedbackEventType,
    FeedbackSource,
    SignalStrength,
)
from patchfrog.persistence.models.context import ContextBundleModel, ContextBundleStatus
from patchfrog.persistence.models.repository_index import IndexStatus, RepositoryIndexModel
from patchfrog.persistence.repositories import (
    AIFindingProposalRepository,
    AIFindingRepository,
    CriticVerdictRepository,
    RepositoryRepository,
    ReviewCandidateRepository,
    ReviewRunRepository,
)
from patchfrog.persistence.repositories.feedback import FeedbackEventRepository
from patchfrog.review.agents.roles import AgentRole
from patchfrog.review.domain import (
    AIReviewFinding,
    CriticDecision,
    CriticVerdict,
    FinalAIFinding,
    ProposalStatus,
    ReviewCandidate,
    ReviewCandidateReason,
    ReviewEvidence,
    ReviewRunStatus,
)
from patchfrog.review.effort_types import ReviewEffortReason, ReviewEffortTier
from patchfrog.telemetry.aggregation import (
    compute_feedback_coverage,
    compute_quality_funnel,
    compute_review_feedback_summary,
    compute_role_funnel,
    compute_tier_distribution,
    compute_tier_funnel,
)
from patchfrog.telemetry.collector import collect_review_telemetry
from patchfrog.telemetry.domain import FindingLifecycleOutcome
from patchfrog.telemetry.reporting import snapshot_to_dict
from tests.support.publishing import scripted_findings_response, setup_reviewed_pull_request

#: Sentinel content planted in fields telemetry must never expose --
#: never a real credential, and deliberately not shaped like one (no
#: real provider prefix, no numeric-id-shaped segment) so it can never
#: be mistaken by secret-scanning push protection for a real key --
#: just an obviously-fake, greppable marker.
_SECRET_SENTINEL = "TELEMETRY_REDACTION_SENTINEL_NOT_A_REAL_SECRET_998877"
_CONTEXT_SECRET_SENTINEL = "TELEMETRY_CONTEXT_SENTINEL_NOT_A_REAL_SECRET_112233"


@dataclass(frozen=True, slots=True)
class _Scenario:
    review_run_id: uuid.UUID
    repository_id: uuid.UUID
    candidate_light_id: uuid.UUID
    candidate_deep_id: uuid.UUID
    candidate_historical_id: uuid.UUID
    finding_accepted_id: uuid.UUID
    finding_downgraded_id: uuid.UUID
    context_bundle_adaptive_id: uuid.UUID
    context_bundle_historical_id: uuid.UUID


def _candidate(*, file_path: str = "a.py", start_line: int = 1, end_line: int = 5) -> ReviewCandidate:
    return ReviewCandidate(
        file_path=file_path, symbol_id=None, symbol_name="fn", qualified_name="mod.fn",
        start_line=start_line, end_line=end_line, changed_lines=(start_line,), static_finding_ids=(),
        reason=ReviewCandidateReason.CHANGED_SYMBOL,
    )


def _finding(
    *, title: str = "t", message: str = "m", severity: Severity = Severity.MEDIUM,
    quoted: str = "q", file_path: str = "a.py", line: int = 1,
) -> AIReviewFinding:
    return AIReviewFinding(
        title=title, message=message, category=FindingCategory.CORRECTNESS, severity=severity,
        confidence=Confidence.HIGH, file_path=file_path, start_line=line, end_line=line,
        evidence=(ReviewEvidence(file_path=file_path, start_line=line, end_line=line, quoted_text=quoted),),
        reasoning_summary="r", suggested_fix=None, impact=None,
    )


async def _persist_scenario(session_factory: async_sessionmaker[AsyncSession]) -> _Scenario:
    async with session_factory() as session:
        repo_row = await RepositoryRepository().upsert(
            session, github_repository_id=uuid.uuid4().int & 0x7FFFFFFFFFFFFFFF, owner="t", name="telemetry-scenario",
            full_name="t/telemetry-scenario", installation_id=0,
        )
        repository_id = repo_row.id

        index_row = RepositoryIndexModel(
            repository_id=repository_id, commit_sha="a" * 40, index_version=1, status=IndexStatus.SUCCEEDED,
            is_active=True, started_at=datetime.now(UTC), completed_at=datetime.now(UTC),
        )
        session.add(index_row)
        await session.flush()
        repository_index_id = index_row.id

        run_model, _ = await ReviewRunRepository().get_or_create_running(
            session, repository_id=repository_id, repository_index_id=repository_index_id, commit_sha="a" * 40,
            config_fingerprint="cfg", model_fingerprint="model", reviewer_provider="fake", reviewer_model="fake-1",
            critic_provider="fake", critic_model="fake-critic",
        )
        review_run_id = run_model.id

        candidate_repo = ReviewCandidateRepository()
        candidate_light = await candidate_repo.create(
            session, review_run_id=review_run_id, candidate=_candidate(file_path="light.py"),
            effort_tier=ReviewEffortTier.LIGHT, effort_reasons=(ReviewEffortReason.NO_SIGNAL,),
        )
        candidate_deep = await candidate_repo.create(
            session, review_run_id=review_run_id, candidate=_candidate(file_path="deep.py"),
            effort_tier=ReviewEffortTier.DEEP, effort_reasons=(ReviewEffortReason.SECURITY_RELEVANT,),
            escalated=True, escalation_reason=ReviewEffortReason.HIGH_RISK_PROPOSAL,
        )
        # Historical row: no effort tier at all -- predates Quality + Cost
        # Guard (spec section 44). Never fabricate a tier for this.
        candidate_historical = await candidate_repo.create(
            session, review_run_id=review_run_id, candidate=_candidate(file_path="historical.py"),
        )

        # Context bundles: one real adaptive-expansion bundle (linked to
        # the DEEP candidate), one historical-shaped bundle (attempted
        # always False, linked to the LIGHT candidate) -- also plants the
        # context-content redaction sentinel via ContextItemModel, added
        # below once bundle ids exist.
        bundle_adaptive = ContextBundleModel(
            repository_id=repository_id, repository_index_id=repository_index_id, commit_sha="a" * 40,
            target_type=ContextTargetType.SYMBOL, target_file_path="deep.py", target_fingerprint="fp-deep",
            config_fingerprint="ctxcfg", engine_version=2, status=ContextBundleStatus.SUCCEEDED,
            total_tokens=120, total_lines=30, generation_ms=12.5,
            adaptive_expansion_attempted=True, adaptive_expansion_occurred=True,
            adaptive_expansion_reasons=json.dumps(["call_chain_continuation", "changed_neighbor"]),
            adaptive_expansion_direction="callers", adaptive_requested_max_depth=2, adaptive_effective_max_depth=2,
            depth_2_candidate_count=4, depth_2_selected_count=2, depth_2_tokens=40,
            started_at=datetime.now(UTC), completed_at=datetime.now(UTC),
        )
        bundle_historical = ContextBundleModel(
            repository_id=repository_id, repository_index_id=repository_index_id, commit_sha="a" * 40,
            target_type=ContextTargetType.SYMBOL, target_file_path="light.py", target_fingerprint="fp-light",
            config_fingerprint="ctxcfg2", engine_version=1, status=ContextBundleStatus.SUCCEEDED,
            total_tokens=30, total_lines=10, generation_ms=None,
            started_at=datetime.now(UTC), completed_at=datetime.now(UTC),
        )
        session.add_all([bundle_adaptive, bundle_historical])
        await session.flush()

        from patchfrog.persistence.models.context import ContextItemModel

        session.add(
            ContextItemModel(
                bundle_id=bundle_adaptive.id, rank=0, kind="caller", relationship="direct_caller", distance=1,
                file_path="deep.py", start_line=1, end_line=5,
                content=f"def caller():\n    api_key = '{_CONTEXT_SECRET_SENTINEL}'\n    return deep()\n",
                content_hash="h", truncated=False, score=1.0, score_breakdown="[]", estimated_tokens=20,
                reason="direct caller",
            )
        )
        await session.flush()

        await candidate_repo.mark_reviewed(session, candidate_id=candidate_light.id, context_bundle_id=bundle_historical.id)
        await candidate_repo.mark_reviewed(session, candidate_id=candidate_deep.id, context_bundle_id=bundle_adaptive.id)
        await candidate_repo.mark_reviewed(session, candidate_id=candidate_historical.id, context_bundle_id=None)

        proposal_repo = AIFindingProposalRepository()
        verdict_repo = CriticVerdictRepository()
        finding_repo = AIFindingRepository()

        def _verdict(decision: CriticDecision, **kwargs: object) -> CriticVerdict:
            return CriticVerdict(
                decision=decision, reasoning_summary="critic reasoning", provider="fake", model="fake-critic",
                input_tokens=15, output_tokens=6, thinking_tokens=2, latency_ms=40.0,
                **kwargs,  # type: ignore[arg-type]
            )

        # 1. Validation-rejected (Correctness) -- secret sentinel planted
        #    on message/evidence/reasoning fields (never read by telemetry).
        rejected_finding = _finding(
            title="rejected", message=_SECRET_SENTINEL, quoted=_SECRET_SENTINEL, file_path="light.py",
        )
        await proposal_repo.create(
            session, review_run_id=review_run_id, candidate_id=candidate_light.id, finding=rejected_finding,
            status=ProposalStatus.REJECTED_VALIDATION, validation_detail="hallucinated",
            agent_role=AgentRole.CORRECTNESS, validation_outcome=None,  # simulates a historical row
        )

        # 2. Critic-rejected (Security).
        critic_rejected_finding = _finding(title="critic-rejected", file_path="deep.py")
        p2 = await proposal_repo.create(
            session, review_run_id=review_run_id, candidate_id=candidate_deep.id, finding=critic_rejected_finding,
            status=ProposalStatus.REJECTED_CRITIC, validation_detail="critic reasoning",
            agent_role=AgentRole.SECURITY,
        )
        await verdict_repo.create(session, proposal_id=p2.id, verdict=_verdict(CriticDecision.REJECT))

        # 3. Below-confidence-threshold.
        p3_finding = _finding(title="low-confidence", file_path="deep.py")
        await proposal_repo.create(
            session, review_run_id=review_run_id, candidate_id=candidate_deep.id, finding=p3_finding,
            status=ProposalStatus.REJECTED_LOW_CONFIDENCE, validation_detail="below minimum",
            agent_role=AgentRole.CORRECTNESS,
        )

        # 4. Suppressed-duplicate.
        p4_finding = _finding(title="dup", file_path="deep.py")
        await proposal_repo.create(
            session, review_run_id=review_run_id, candidate_id=candidate_deep.id, finding=p4_finding,
            status=ProposalStatus.SUPPRESSED_DUPLICATE, validation_detail="cross-role duplicate",
            agent_role=AgentRole.SECURITY,
        )

        # 5. Suppressed-contradiction.
        p5_finding = _finding(title="contradiction", file_path="deep.py")
        p5 = await proposal_repo.create(
            session, review_run_id=review_run_id, candidate_id=candidate_deep.id, finding=p5_finding,
            status=ProposalStatus.SUPPRESSED_CONTRADICTION, validation_detail="unresolved cross-role contradiction",
            agent_role=AgentRole.CORRECTNESS,
        )
        await verdict_repo.create(session, proposal_id=p5.id, verdict=_verdict(CriticDecision.REJECT))

        # 6. Suppressed-budget.
        p6_finding = _finding(title="budget", file_path="deep.py")
        await proposal_repo.create(
            session, review_run_id=review_run_id, candidate_id=candidate_deep.id, finding=p6_finding,
            status=ProposalStatus.SUPPRESSED_BUDGET, validation_detail="critic budget exhausted",
            agent_role=AgentRole.SECURITY,
        )

        # 7. Accepted final (Correctness), with a plain-accept critic verdict.
        accepted_finding = _finding(title="accepted", file_path="deep.py", severity=Severity.HIGH)
        p7 = await proposal_repo.create(
            session, review_run_id=review_run_id, candidate_id=candidate_deep.id, finding=accepted_finding,
            status=ProposalStatus.ACCEPTED, validation_detail=None, agent_role=AgentRole.CORRECTNESS,
        )
        await verdict_repo.create(session, proposal_id=p7.id, verdict=_verdict(CriticDecision.ACCEPT))
        [finding_model_accepted] = await finding_repo.bulk_create(
            session, review_run_id=review_run_id,
            findings=[
                FinalAIFinding(
                    proposal_id=p7.id, candidate_id=candidate_deep.id, candidate=_candidate(file_path="deep.py"),
                    finding=accepted_finding, critic_verdict=None, final_severity=Severity.HIGH,
                    final_confidence=Confidence.HIGH, corroborated_by_static=False, static_finding_ids=(),
                    agent_role=AgentRole.CORRECTNESS,
                )
            ],
        )

        # 8. Accepted + critic downgrade -> CRITIC_DOWNGRADED.
        downgraded_finding = _finding(title="downgraded", file_path="deep.py", severity=Severity.HIGH)
        p8 = await proposal_repo.create(
            session, review_run_id=review_run_id, candidate_id=candidate_deep.id, finding=downgraded_finding,
            status=ProposalStatus.ACCEPTED, validation_detail=None, agent_role=AgentRole.SECURITY,
        )
        await verdict_repo.create(
            session, proposal_id=p8.id,
            verdict=_verdict(CriticDecision.DOWNGRADE, downgraded_severity=Severity.MEDIUM, downgraded_confidence=Confidence.MEDIUM),
        )
        [finding_model_downgraded] = await finding_repo.bulk_create(
            session, review_run_id=review_run_id,
            findings=[
                FinalAIFinding(
                    proposal_id=p8.id, candidate_id=candidate_deep.id, candidate=_candidate(file_path="deep.py"),
                    finding=downgraded_finding, critic_verdict=None, final_severity=Severity.MEDIUM,
                    final_confidence=Confidence.MEDIUM, corroborated_by_static=False, static_finding_ids=(),
                    agent_role=AgentRole.SECURITY,
                )
            ],
        )

        await ReviewRunRepository().mark_succeeded(
            session, run_id=review_run_id, status=ReviewRunStatus.SUCCEEDED, candidate_count=3,
            candidates_reviewed=3, candidates_failed=0, candidates_skipped_budget=0, proposals_count=8,
            accepted_count=2, rejected_count=4, suppressed_duplicate_count=1,
            reviewer_input_tokens=300, reviewer_output_tokens=100, critic_input_tokens=60, critic_output_tokens=24,
            correctness_input_tokens=180, correctness_output_tokens=60, security_input_tokens=120,
            security_output_tokens=40, correctness_thinking_tokens=5, security_thinking_tokens=3,
            reviewer_thinking_tokens=8, critic_thinking_tokens=8, candidates_by_tier={
                ReviewEffortTier.LIGHT: 1, ReviewEffortTier.DEEP: 1,
            }, candidates_escalated=1, critic_calls=4, retries_consumed=2, reviewer_latency_ms=777.0,
            calls_by_role={AgentRole.CORRECTNESS: 3, AgentRole.SECURITY: 3}, duration_ms=250.0,
        )

        # Feedback: useful on the accepted finding, false-positive on the
        # downgraded finding, none on... there is no third published
        # finding here, so add an explicit-fixed event on the accepted
        # finding's sibling by reusing finding_model_downgraded with a
        # FIXED command instead, keeping "no feedback" representable by
        # simply not adding any event for a third published finding is not
        # possible with only two findings -- see the dedicated
        # ``test_feedback_no_events_remains_unknown`` for that case using
        # its own single-finding scenario instead.
        feedback_repo = FeedbackEventRepository()
        await feedback_repo.create_if_new(
            session,
            event=FeedbackEvent(
                repository_id=repository_id, pull_request_id=None, review_run_id=review_run_id,
                publication_id=None, review_publication_comment_id=None, finding_id=finding_model_accepted.id,
                github_review_id=1, github_comment_id=1, event_type=FeedbackEventType.EXPLICIT_COMMAND,
                source=FeedbackSource.REPLY_SYNC, external_event_id="cmd:1",
                raw_signal=ExplicitCommand.USEFUL.value, normalized_signal=ExplicitCommand.USEFUL.value,
                signal_strength=SignalStrength.STRONG, actor=ActorIdentity(login="dev", is_bot=False),
                occurred_at=datetime.now(UTC),
            ),
        )
        await feedback_repo.create_if_new(
            session,
            event=FeedbackEvent(
                repository_id=repository_id, pull_request_id=None, review_run_id=review_run_id,
                publication_id=None, review_publication_comment_id=None, finding_id=finding_model_downgraded.id,
                github_review_id=1, github_comment_id=2, event_type=FeedbackEventType.EXPLICIT_COMMAND,
                source=FeedbackSource.REPLY_SYNC, external_event_id="cmd:2",
                raw_signal=ExplicitCommand.FALSE_POSITIVE.value, normalized_signal=ExplicitCommand.FALSE_POSITIVE.value,
                signal_strength=SignalStrength.STRONG, actor=ActorIdentity(login="dev", is_bot=False),
                occurred_at=datetime.now(UTC),
            ),
        )
        await session.commit()

        return _Scenario(
            review_run_id=review_run_id, repository_id=repository_id, candidate_light_id=candidate_light.id,
            candidate_deep_id=candidate_deep.id, candidate_historical_id=candidate_historical.id,
            finding_accepted_id=finding_model_accepted.id, finding_downgraded_id=finding_model_downgraded.id,
            context_bundle_adaptive_id=bundle_adaptive.id, context_bundle_historical_id=bundle_historical.id,
        )


async def test_collect_snapshot_matches_run(session_factory: async_sessionmaker[AsyncSession]) -> None:
    scenario = await _persist_scenario(session_factory)
    async with session_factory() as session:
        snapshot = await collect_review_telemetry(session, review_run_id=scenario.review_run_id)
    assert snapshot is not None
    assert snapshot.review_run_id == scenario.review_run_id
    assert snapshot.repository_id == scenario.repository_id
    assert snapshot.status == "succeeded"
    assert len(snapshot.finding_lifecycle) == 8
    assert len(snapshot.candidates) == 3


async def test_collect_returns_none_for_unknown_run(session_factory: async_sessionmaker[AsyncSession]) -> None:
    async with session_factory() as session:
        snapshot = await collect_review_telemetry(session, review_run_id=uuid.uuid4())
    assert snapshot is None


async def test_collection_is_deterministic_for_same_state(session_factory: async_sessionmaker[AsyncSession]) -> None:
    scenario = await _persist_scenario(session_factory)
    async with session_factory() as session:
        first = await collect_review_telemetry(session, review_run_id=scenario.review_run_id)
    async with session_factory() as session:
        second = await collect_review_telemetry(session, review_run_id=scenario.review_run_id)
    assert first == second


async def test_collection_never_mutates_review_state(session_factory: async_sessionmaker[AsyncSession]) -> None:
    scenario = await _persist_scenario(session_factory)
    async with session_factory() as session:
        run_before = await ReviewRunRepository().get_by_id(session, run_id=scenario.review_run_id)
        assert run_before is not None
        status_before, count_before = run_before.status, run_before.proposals_count

    async with session_factory() as session:
        await collect_review_telemetry(session, review_run_id=scenario.review_run_id)
        await collect_review_telemetry(session, review_run_id=scenario.review_run_id)

    async with session_factory() as session:
        run_after = await ReviewRunRepository().get_by_id(session, run_id=scenario.review_run_id)
        assert run_after is not None
        assert run_after.status == status_before
        assert run_after.proposals_count == count_before


async def test_role_provenance_correctness_and_security(session_factory: async_sessionmaker[AsyncSession]) -> None:
    scenario = await _persist_scenario(session_factory)
    async with session_factory() as session:
        snapshot = await collect_review_telemetry(session, review_run_id=scenario.review_run_id)
    assert snapshot is not None
    roles = {e.agent_role for e in snapshot.finding_lifecycle}
    assert AgentRole.CORRECTNESS in roles
    assert AgentRole.SECURITY in roles


@pytest.mark.parametrize(
    "expected_outcome",
    [
        FindingLifecycleOutcome.VALIDATION_REJECTED,
        FindingLifecycleOutcome.CRITIC_REJECTED,
        FindingLifecycleOutcome.BELOW_CONFIDENCE_THRESHOLD,
        FindingLifecycleOutcome.SUPPRESSED_DUPLICATE,
        FindingLifecycleOutcome.SUPPRESSED_CONTRADICTION,
        FindingLifecycleOutcome.SUPPRESSED_BUDGET,
        FindingLifecycleOutcome.ACCEPTED_FINAL,
        FindingLifecycleOutcome.CRITIC_DOWNGRADED,
    ],
)
async def test_every_lifecycle_outcome_is_classified_from_real_persisted_state(
    session_factory: async_sessionmaker[AsyncSession], expected_outcome: FindingLifecycleOutcome
) -> None:
    scenario = await _persist_scenario(session_factory)
    async with session_factory() as session:
        snapshot = await collect_review_telemetry(session, review_run_id=scenario.review_run_id)
    assert snapshot is not None
    matches = [e for e in snapshot.finding_lifecycle if e.outcome is expected_outcome]
    assert matches, [e.outcome for e in snapshot.finding_lifecycle]


async def test_tier_counts_and_escalation(session_factory: async_sessionmaker[AsyncSession]) -> None:
    scenario = await _persist_scenario(session_factory)
    async with session_factory() as session:
        snapshot = await collect_review_telemetry(session, review_run_id=scenario.review_run_id)
    assert snapshot is not None
    distribution = compute_tier_distribution(snapshot.candidates)
    assert distribution.tier_counts == {"light": 1, "deep": 1}
    assert distribution.escalated == 1

    deep_candidate = next(c for c in snapshot.candidates if c.candidate_id == scenario.candidate_deep_id)
    assert deep_candidate.escalated is True
    assert deep_candidate.escalation_reason is ReviewEffortReason.HIGH_RISK_PROPOSAL


async def test_reviewer_and_critic_token_counts(session_factory: async_sessionmaker[AsyncSession]) -> None:
    scenario = await _persist_scenario(session_factory)
    async with session_factory() as session:
        snapshot = await collect_review_telemetry(session, review_run_id=scenario.review_run_id)
    assert snapshot is not None
    provider = snapshot.provider
    assert provider.reviewer_input_tokens_total == 300
    assert provider.reviewer_output_tokens_total == 100
    assert provider.critic_input_tokens_total == 60
    assert provider.critic_output_tokens_total == 24
    by_role = {r.role: r for r in provider.reviewer_by_role}
    assert by_role[AgentRole.CORRECTNESS].calls == 3
    assert by_role[AgentRole.SECURITY].calls == 3
    assert by_role[AgentRole.CORRECTNESS].input_tokens == 180
    assert by_role[AgentRole.SECURITY].input_tokens == 120


async def test_thinking_tokens_are_captured(session_factory: async_sessionmaker[AsyncSession]) -> None:
    scenario = await _persist_scenario(session_factory)
    async with session_factory() as session:
        snapshot = await collect_review_telemetry(session, review_run_id=scenario.review_run_id)
    assert snapshot is not None
    assert snapshot.provider.reviewer_thinking_tokens_total == 8
    # 4 critic verdicts persisted, each with thinking_tokens=2.
    assert snapshot.provider.critic_thinking_tokens_total == 8


async def test_retries_consumed_is_captured(session_factory: async_sessionmaker[AsyncSession]) -> None:
    scenario = await _persist_scenario(session_factory)
    async with session_factory() as session:
        snapshot = await collect_review_telemetry(session, review_run_id=scenario.review_run_id)
    assert snapshot is not None
    assert snapshot.provider.retries_consumed == 2


async def test_reviewer_and_critic_latency_aggregates_are_never_confused_with_wall_clock(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    scenario = await _persist_scenario(session_factory)
    async with session_factory() as session:
        snapshot = await collect_review_telemetry(session, review_run_id=scenario.review_run_id)
    assert snapshot is not None
    assert snapshot.provider.reviewer_latency_ms_aggregate == 777.0
    # 4 critic verdicts persisted, each latency_ms=40.0.
    assert snapshot.provider.critic_latency_ms_aggregate == 160.0
    # Both aggregates legitimately exceed the run's own wall-clock
    # duration_ms (250.0) -- the whole point of keeping them distinct.
    assert snapshot.duration_ms == 250.0
    assert snapshot.provider.reviewer_latency_ms_aggregate > snapshot.duration_ms


async def test_adaptive_context_metrics_are_captured(session_factory: async_sessionmaker[AsyncSession]) -> None:
    scenario = await _persist_scenario(session_factory)
    async with session_factory() as session:
        snapshot = await collect_review_telemetry(session, review_run_id=scenario.review_run_id)
    assert snapshot is not None
    adaptive_bundle = next(c for c in snapshot.context if c.bundle_id == scenario.context_bundle_adaptive_id)
    assert adaptive_bundle.adaptive_attempted is True
    assert adaptive_bundle.adaptive_occurred is True
    assert adaptive_bundle.adaptive_direction == "callers"
    assert {r.value for r in adaptive_bundle.adaptive_reasons} == {"call_chain_continuation", "changed_neighbor"}
    assert adaptive_bundle.depth_2_candidate_count == 4
    assert adaptive_bundle.depth_2_selected_count == 2
    assert adaptive_bundle.depth_2_tokens == 40

    historical_bundle = next(c for c in snapshot.context if c.bundle_id == scenario.context_bundle_historical_id)
    assert historical_bundle.adaptive_attempted is False
    assert historical_bundle.adaptive_occurred is False


async def test_feedback_useful_false_positive_and_no_feedback(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    scenario = await _persist_scenario(session_factory)
    async with session_factory() as session:
        snapshot = await collect_review_telemetry(session, review_run_id=scenario.review_run_id)
    assert snapshot is not None
    by_id = {f.finding_id: f for f in snapshot.feedback}
    assert by_id[scenario.finding_accepted_id].has_feedback is True
    assert by_id[scenario.finding_accepted_id].explicit_useful == 1
    assert by_id[scenario.finding_downgraded_id].has_feedback is True
    assert by_id[scenario.finding_downgraded_id].explicit_false_positive == 1


async def test_feedback_no_events_remains_unknown(session_factory: async_sessionmaker[AsyncSession]) -> None:
    """A separate, single-finding scenario for the "no feedback at all"
    case -- distinct from ``test_feedback_useful_false_positive_and_no_feedback``
    because every finding in the shared scenario above intentionally has
    feedback."""

    async with session_factory() as session:
        repo_row = await RepositoryRepository().upsert(
            session, github_repository_id=uuid.uuid4().int & 0x7FFFFFFFFFFFFFFF, owner="t", name="no-fb",
            full_name="t/no-fb", installation_id=0,
        )
        index_row = RepositoryIndexModel(
            repository_id=repo_row.id, commit_sha="b" * 40, index_version=1, status=IndexStatus.SUCCEEDED,
            is_active=True, started_at=datetime.now(UTC), completed_at=datetime.now(UTC),
        )
        session.add(index_row)
        await session.flush()
        run_model, _ = await ReviewRunRepository().get_or_create_running(
            session, repository_id=repo_row.id, repository_index_id=index_row.id, commit_sha="b" * 40,
            config_fingerprint="c2", model_fingerprint="m2", reviewer_provider="fake", reviewer_model="fake-1",
            critic_provider=None, critic_model=None,
        )
        candidate = await ReviewCandidateRepository().create(
            session, review_run_id=run_model.id, candidate=_candidate(), effort_tier=ReviewEffortTier.STANDARD,
        )
        finding = _finding(title="solo")
        proposal = await AIFindingProposalRepository().create(
            session, review_run_id=run_model.id, candidate_id=candidate.id, finding=finding,
            status=ProposalStatus.ACCEPTED, validation_detail=None, agent_role=AgentRole.CORRECTNESS,
        )
        [finding_model] = await AIFindingRepository().bulk_create(
            session, review_run_id=run_model.id,
            findings=[
                FinalAIFinding(
                    proposal_id=proposal.id, candidate_id=candidate.id, candidate=_candidate(), finding=finding,
                    critic_verdict=None, final_severity=Severity.MEDIUM, final_confidence=Confidence.HIGH,
                    corroborated_by_static=False, static_finding_ids=(), agent_role=AgentRole.CORRECTNESS,
                )
            ],
        )
        await ReviewRunRepository().mark_succeeded(
            session, run_id=run_model.id, status=ReviewRunStatus.SUCCEEDED, candidate_count=1,
            candidates_reviewed=1, candidates_failed=0, candidates_skipped_budget=0, proposals_count=1,
            accepted_count=1, rejected_count=0, suppressed_duplicate_count=0, reviewer_input_tokens=10,
            reviewer_output_tokens=5, critic_input_tokens=0, critic_output_tokens=0, duration_ms=10.0,
        )
        await session.commit()
        review_run_id = run_model.id

    async with session_factory() as session:
        snapshot = await collect_review_telemetry(session, review_run_id=review_run_id)
    assert snapshot is not None
    assert len(snapshot.feedback) == 1
    assert snapshot.feedback[0].finding_id == finding_model.id
    assert snapshot.feedback[0].has_feedback is False
    assert snapshot.feedback[0].usefulness_signal is None

    coverage = compute_feedback_coverage(snapshot.feedback)
    assert coverage.coverage_rate == 0.0


async def test_feedback_coverage_calculation_over_scenario(session_factory: async_sessionmaker[AsyncSession]) -> None:
    scenario = await _persist_scenario(session_factory)
    async with session_factory() as session:
        snapshot = await collect_review_telemetry(session, review_run_id=scenario.review_run_id)
    assert snapshot is not None
    coverage = compute_feedback_coverage(snapshot.feedback)
    assert coverage.published_findings == 2
    assert coverage.feedback_bearing_findings == 2
    assert coverage.coverage_rate == 1.0
    assert coverage.user_reported_false_positive_rate == 0.5


async def test_quality_funnel_counts_every_proposal_exactly_once(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    scenario = await _persist_scenario(session_factory)
    async with session_factory() as session:
        snapshot = await collect_review_telemetry(session, review_run_id=scenario.review_run_id)
    assert snapshot is not None
    funnel = compute_quality_funnel(
        candidate_count=snapshot.candidate_count, entries=snapshot.finding_lifecycle, feedback=snapshot.feedback,
    )
    assert funnel.proposals == 8
    assert funnel.accepted_final == 2
    assert sum(funnel.drop_off.values()) + funnel.accepted_final == funnel.proposals
    assert funnel.published_findings == 2
    assert funnel.feedback_bearing_findings == 2


async def test_role_and_tier_funnel_over_scenario(session_factory: async_sessionmaker[AsyncSession]) -> None:
    scenario = await _persist_scenario(session_factory)
    async with session_factory() as session:
        snapshot = await collect_review_telemetry(session, review_run_id=scenario.review_run_id)
    assert snapshot is not None
    role_funnel = {f.key: f for f in compute_role_funnel(snapshot.finding_lifecycle)}
    assert role_funnel["correctness"].proposed + role_funnel["security"].proposed == 8

    tier_funnel = {f.key: f for f in compute_tier_funnel(snapshot.finding_lifecycle)}
    assert tier_funnel["deep"].proposed == 7
    assert tier_funnel["light"].proposed == 1


async def test_historical_nullable_rows_are_supported(session_factory: async_sessionmaker[AsyncSession]) -> None:
    scenario = await _persist_scenario(session_factory)
    async with session_factory() as session:
        snapshot = await collect_review_telemetry(session, review_run_id=scenario.review_run_id)
    assert snapshot is not None

    historical_candidate = next(c for c in snapshot.candidates if c.candidate_id == scenario.candidate_historical_id)
    assert historical_candidate.effort_tier is None
    assert historical_candidate.escalation_reason is None

    rejected_entry = next(e for e in snapshot.finding_lifecycle if e.outcome is FindingLifecycleOutcome.VALIDATION_REJECTED)
    # This proposal was deliberately persisted with validation_outcome=None
    # (simulating a pre-migration row) -- never fabricated back into a
    # real ValidationOutcome value.
    assert rejected_entry.validation_outcome is None


async def test_json_export_contains_no_secret_or_content_sentinel(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    scenario = await _persist_scenario(session_factory)
    async with session_factory() as session:
        snapshot = await collect_review_telemetry(session, review_run_id=scenario.review_run_id)
    assert snapshot is not None
    payload = snapshot_to_dict(snapshot)
    dumped = json.dumps(payload)
    assert _SECRET_SENTINEL not in dumped
    assert _CONTEXT_SECRET_SENTINEL not in dumped
    assert "reasoning" not in dumped.lower()  # no reasoning_summary field anywhere
    assert "quoted_text" not in dumped
    assert payload["schema_version"] == 1


async def test_collector_query_count_does_not_scale_linearly_with_proposal_count(
    session_factory: async_sessionmaker[AsyncSession], db_engine: AsyncEngine,
) -> None:
    scenario = await _persist_scenario(session_factory)

    queries: list[str] = []

    def _count(*_args: object, **_kwargs: object) -> None:
        queries.append("q")

    sync_engine = db_engine.sync_engine
    event.listen(sync_engine, "before_cursor_execute", _count)
    try:
        async with session_factory() as session:
            await collect_review_telemetry(session, review_run_id=scenario.review_run_id)
    finally:
        event.remove(sync_engine, "before_cursor_execute", _count)

    # 8 proposals, 3 candidates, 2 context bundles, 2 findings, 2 feedback
    # events persisted -- a fixed, small number of queries regardless of
    # those counts (never one query per row) is the whole point (spec
    # section 43).
    assert len(queries) <= 15, queries


async def test_collector_failure_never_affects_a_completed_review(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    scenario = await _persist_scenario(session_factory)
    async with session_factory() as session:
        run_before = await ReviewRunRepository().get_by_id(session, run_id=scenario.review_run_id)
        assert run_before is not None
        proposals_before = run_before.proposals_count
        accepted_before = run_before.accepted_count

    # Collecting for a bogus id never raises and never touches anything.
    async with session_factory() as session:
        result = await collect_review_telemetry(session, review_run_id=uuid.uuid4())
    assert result is None

    async with session_factory() as session:
        run_after = await ReviewRunRepository().get_by_id(session, run_id=scenario.review_run_id)
    assert run_after is not None
    assert run_after.proposals_count == proposals_before
    assert run_after.accepted_count == accepted_before


async def test_collector_against_a_real_review_pipeline_run(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    """Guards against the hand-rolled scenario above drifting from what
    the real cooperative-orchestration pipeline actually persists."""

    reviewed = await setup_reviewed_pull_request(
        session_factory, full_name="t/telemetry-real-pipeline", changed_lines=[14],
        response_factory=lambda _req: scripted_findings_response([
            {
                "title": "Inverted comparison", "message": "backwards comparison", "category": "correctness",
                "severity": "medium", "confidence": "medium", "file_path": "src/billing.py", "start_line": 14,
                "end_line": 14, "evidence": [
                    {"file_path": "src/billing.py", "start_line": 14, "end_line": 14, "quoted_text": "return amount >= balance"}
                ],
                "reasoning_summary": "deterministic test finding", "suggested_fix": None,
            }
        ]),
        tmp_root=tmp_path,
    )
    async with session_factory() as session:
        snapshot = await collect_review_telemetry(session, review_run_id=reviewed.review_run_id)
    assert snapshot is not None
    assert snapshot.review_run_id == reviewed.review_run_id
    assert snapshot.candidate_count > 0
    assert len(snapshot.finding_lifecycle) >= 1
    real_finding_ids = {f.id for f in reviewed.findings}
    telemetry_finding_ids = {e.finding_id for e in snapshot.finding_lifecycle if e.finding_id is not None}
    assert real_finding_ids == telemetry_finding_ids
    assert snapshot.provider.reviewer_input_tokens_total > 0


# ---------------------------------------------------------------------------
# Review-scoped (unattributed) feedback -- FeedbackEvent.finding_id is best-
# effort and may be None (see patchfrog.feedback.attribution). Telemetry must
# preserve that ambiguity rather than dropping the event or forcing it onto a
# finding it was never confirmed to be about.
# ---------------------------------------------------------------------------

_REVIEW_FEEDBACK_SENTINEL = "TELEMETRY_REVIEW_FEEDBACK_SENTINEL_NOT_A_REAL_SECRET_554433"


@dataclass(frozen=True, slots=True)
class _ReviewFeedbackScenario:
    review_run_id: uuid.UUID
    finding_id: uuid.UUID


async def _persist_review_scoped_feedback_scenario(
    session_factory: async_sessionmaker[AsyncSession],
) -> _ReviewFeedbackScenario:
    """One published finding with its own explicit feedback, plus three
    review-level events that could never be attributed to it or to any
    other finding: two *conflicting* explicit commands (one on each of
    two review-publication-comment-less contexts) and one PR-lifecycle
    event, which structurally never carries a ``finding_id`` at all."""

    async with session_factory() as session:
        repo_row = await RepositoryRepository().upsert(
            session, github_repository_id=uuid.uuid4().int & 0x7FFFFFFFFFFFFFFF, owner="t", name="review-fb",
            full_name="t/review-fb", installation_id=0,
        )
        index_row = RepositoryIndexModel(
            repository_id=repo_row.id, commit_sha="d" * 40, index_version=1, status=IndexStatus.SUCCEEDED,
            is_active=True, started_at=datetime.now(UTC), completed_at=datetime.now(UTC),
        )
        session.add(index_row)
        await session.flush()
        run_model, _ = await ReviewRunRepository().get_or_create_running(
            session, repository_id=repo_row.id, repository_index_id=index_row.id, commit_sha="d" * 40,
            config_fingerprint="c3", model_fingerprint="m3", reviewer_provider="fake", reviewer_model="fake-1",
            critic_provider=None, critic_model=None,
        )
        candidate = await ReviewCandidateRepository().create(
            session, review_run_id=run_model.id, candidate=_candidate(), effort_tier=ReviewEffortTier.STANDARD,
        )
        finding = _finding(title="attributed")
        proposal = await AIFindingProposalRepository().create(
            session, review_run_id=run_model.id, candidate_id=candidate.id, finding=finding,
            status=ProposalStatus.ACCEPTED, validation_detail=None, agent_role=AgentRole.CORRECTNESS,
        )
        [finding_model] = await AIFindingRepository().bulk_create(
            session, review_run_id=run_model.id,
            findings=[
                FinalAIFinding(
                    proposal_id=proposal.id, candidate_id=candidate.id, candidate=_candidate(), finding=finding,
                    critic_verdict=None, final_severity=Severity.MEDIUM, final_confidence=Confidence.HIGH,
                    corroborated_by_static=False, static_finding_ids=(), agent_role=AgentRole.CORRECTNESS,
                )
            ],
        )
        await ReviewRunRepository().mark_succeeded(
            session, run_id=run_model.id, status=ReviewRunStatus.SUCCEEDED, candidate_count=1,
            candidates_reviewed=1, candidates_failed=0, candidates_skipped_budget=0, proposals_count=1,
            accepted_count=1, rejected_count=0, suppressed_duplicate_count=0, reviewer_input_tokens=10,
            reviewer_output_tokens=5, critic_input_tokens=0, critic_output_tokens=0, duration_ms=10.0,
        )

        feedback_repo = FeedbackEventRepository()
        actor = ActorIdentity(login="dev", is_bot=False)

        # Finding-scoped: attributed normally.
        await feedback_repo.create_if_new(
            session,
            event=FeedbackEvent(
                repository_id=repo_row.id, pull_request_id=None, review_run_id=run_model.id, publication_id=None,
                review_publication_comment_id=None, finding_id=finding_model.id, github_review_id=1,
                github_comment_id=1, event_type=FeedbackEventType.EXPLICIT_COMMAND, source=FeedbackSource.REPLY_SYNC,
                external_event_id="cmd:attributed", raw_signal=ExplicitCommand.USEFUL.value,
                normalized_signal=ExplicitCommand.USEFUL.value, signal_strength=SignalStrength.STRONG, actor=actor,
                occurred_at=datetime.now(UTC),
            ),
        )
        # Review-scoped: attribution failed -- finding_id=None, but still a
        # real, structured signal that must be preserved, not dropped.
        # Plants a sentinel in raw_signal/metadata (never actually
        # populated with free text by patchfrog.feedback.sync, but tested
        # here as defense-in-depth) to prove it can never leak.
        await feedback_repo.create_if_new(
            session,
            event=FeedbackEvent(
                repository_id=repo_row.id, pull_request_id=None, review_run_id=run_model.id, publication_id=None,
                review_publication_comment_id=None, finding_id=None, github_review_id=1, github_comment_id=2,
                event_type=FeedbackEventType.EXPLICIT_COMMAND, source=FeedbackSource.REPLY_SYNC,
                external_event_id="cmd:unattributed-1", raw_signal=_REVIEW_FEEDBACK_SENTINEL,
                normalized_signal=ExplicitCommand.USEFUL.value, signal_strength=SignalStrength.STRONG, actor=actor,
                occurred_at=datetime.now(UTC), metadata={"note": _REVIEW_FEEDBACK_SENTINEL},
            ),
        )
        # A second, conflicting review-scoped event -- must be retained
        # alongside the first, never collapsed into one label.
        await feedback_repo.create_if_new(
            session,
            event=FeedbackEvent(
                repository_id=repo_row.id, pull_request_id=None, review_run_id=run_model.id, publication_id=None,
                review_publication_comment_id=None, finding_id=None, github_review_id=1, github_comment_id=3,
                event_type=FeedbackEventType.EXPLICIT_COMMAND, source=FeedbackSource.REPLY_SYNC,
                external_event_id="cmd:unattributed-2", raw_signal=ExplicitCommand.FALSE_POSITIVE.value,
                normalized_signal=ExplicitCommand.FALSE_POSITIVE.value, signal_strength=SignalStrength.STRONG,
                actor=actor, occurred_at=datetime.now(UTC),
            ),
        )
        # A PR-lifecycle event -- structurally never has a finding_id at
        # all, not merely one that failed attribution.
        await feedback_repo.create_if_new(
            session,
            event=FeedbackEvent(
                repository_id=repo_row.id, pull_request_id=None, review_run_id=run_model.id, publication_id=None,
                review_publication_comment_id=None, finding_id=None, github_review_id=None, github_comment_id=None,
                event_type=FeedbackEventType.PR_MERGED, source=FeedbackSource.PR_LIFECYCLE_SYNC,
                external_event_id="pr:merged", raw_signal="merged", normalized_signal="merged",
                signal_strength=SignalStrength.WEAK, actor=actor, occurred_at=datetime.now(UTC),
            ),
        )
        await session.commit()
        return _ReviewFeedbackScenario(review_run_id=run_model.id, finding_id=finding_model.id)


async def test_finding_scoped_feedback_still_appears_under_exact_finding(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    scenario = await _persist_review_scoped_feedback_scenario(session_factory)
    async with session_factory() as session:
        snapshot = await collect_review_telemetry(session, review_run_id=scenario.review_run_id)
    assert snapshot is not None
    assert len(snapshot.feedback) == 1
    assert snapshot.feedback[0].finding_id == scenario.finding_id
    assert snapshot.feedback[0].has_feedback is True
    assert snapshot.feedback[0].explicit_useful == 1
    assert snapshot.feedback[0].scope.value == "finding"


async def test_review_scoped_event_with_no_finding_id_is_preserved(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    scenario = await _persist_review_scoped_feedback_scenario(session_factory)
    async with session_factory() as session:
        snapshot = await collect_review_telemetry(session, review_run_id=scenario.review_run_id)
    assert snapshot is not None
    # 3 review-scoped events persisted above -- none dropped.
    assert len(snapshot.review_feedback) == 3


async def test_review_scoped_feedback_is_not_assigned_to_any_finding(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    scenario = await _persist_review_scoped_feedback_scenario(session_factory)
    async with session_factory() as session:
        snapshot = await collect_review_telemetry(session, review_run_id=scenario.review_run_id)
    assert snapshot is not None
    # ReviewFeedbackEventTelemetry has no finding_id field at all --
    # structurally impossible to misattribute. The one real finding's own
    # feedback entry is unaffected by the 3 review-scoped events.
    assert len(snapshot.feedback) == 1
    assert all(f.scope.value == "review" for f in snapshot.review_feedback)


async def test_feedback_coverage_denominators_ignore_review_scoped_events(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    scenario = await _persist_review_scoped_feedback_scenario(session_factory)
    async with session_factory() as session:
        snapshot = await collect_review_telemetry(session, review_run_id=scenario.review_run_id)
    assert snapshot is not None
    coverage = compute_feedback_coverage(snapshot.feedback)
    # 1 published finding, 1 feedback-bearing -- the 3 review-scoped
    # events never inflate either the numerator or the denominator.
    assert coverage.published_findings == 1
    assert coverage.feedback_bearing_findings == 1
    assert coverage.coverage_rate == 1.0
    assert coverage.useful_rate == 1.0
    assert coverage.user_reported_false_positive_rate == 0.0


async def test_conflicting_review_level_events_are_all_retained_not_collapsed(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """One review-scoped event says "useful", another says "false-
    positive" -- both must survive as distinct, countable events. Never
    collapsed into one fabricated truth label."""

    scenario = await _persist_review_scoped_feedback_scenario(session_factory)
    async with session_factory() as session:
        snapshot = await collect_review_telemetry(session, review_run_id=scenario.review_run_id)
    assert snapshot is not None
    signals = sorted(e.normalized_signal for e in snapshot.review_feedback)
    assert signals == ["false-positive", "merged", "useful"]

    summary = compute_review_feedback_summary(snapshot.review_feedback)
    assert summary.review_feedback_event_count == 3
    assert summary.review_feedback_by_signal == {"useful": 1, "false-positive": 1, "merged": 1}
    assert summary.review_feedback_by_event_type == {"explicit_command": 2, "pr_merged": 1}


async def test_historical_review_scoped_feedback_with_no_finding_id_is_supported(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A PR-lifecycle event has never had a finding_id -- this is not a
    row predating a migration, it is a kind of event that is inherently
    review-scoped. Telemetry must support it identically to a reaction/
    reply whose attribution merely failed."""

    scenario = await _persist_review_scoped_feedback_scenario(session_factory)
    async with session_factory() as session:
        snapshot = await collect_review_telemetry(session, review_run_id=scenario.review_run_id)
    assert snapshot is not None
    merged_events = [e for e in snapshot.review_feedback if e.event_type.value == "pr_merged"]
    assert len(merged_events) == 1
    assert merged_events[0].scope.value == "review"


async def test_review_scoped_feedback_json_export_has_no_secret_and_includes_scope(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    scenario = await _persist_review_scoped_feedback_scenario(session_factory)
    async with session_factory() as session:
        snapshot = await collect_review_telemetry(session, review_run_id=scenario.review_run_id)
    assert snapshot is not None
    payload = snapshot_to_dict(snapshot)
    dumped = json.dumps(payload)

    # Defense-in-depth: even though raw_signal/metadata are never
    # populated with free text by patchfrog.feedback.sync in production,
    # ReviewFeedbackEventTelemetry structurally never reads either field
    # at all -- the sentinel planted in both must never appear.
    assert _REVIEW_FEEDBACK_SENTINEL not in dumped

    assert payload["feedback"][0]["scope"] == "finding"
    review_scopes = {e["scope"] for e in payload["review_feedback"]}
    assert review_scopes == {"review"}


async def test_collector_query_bound_unaffected_by_review_scoped_events(
    session_factory: async_sessionmaker[AsyncSession], db_engine: AsyncEngine,
) -> None:
    scenario = await _persist_review_scoped_feedback_scenario(session_factory)

    queries: list[str] = []

    def _count(*_args: object, **_kwargs: object) -> None:
        queries.append("q")

    sync_engine = db_engine.sync_engine
    event.listen(sync_engine, "before_cursor_execute", _count)
    try:
        async with session_factory() as session:
            await collect_review_telemetry(session, review_run_id=scenario.review_run_id)
    finally:
        event.remove(sync_engine, "before_cursor_execute", _count)

    # Same single get_feedback_for_review() query already covers both
    # finding-scoped and review-scoped events -- no new query is added.
    assert len(queries) <= 15, queries
