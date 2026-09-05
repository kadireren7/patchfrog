from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from patchfrog.change_intelligence.telemetry import ChangeIntelligenceSummary
from patchfrog.contract_intelligence.telemetry import ContractIntelligenceSummary
from patchfrog.historical_regression_memory.telemetry import HistoricalRegressionMemorySummary
from patchfrog.intent_verification.telemetry import IntentVerificationSummary
from patchfrog.persistence.models.review import ReviewRunModel
from patchfrog.review.agents.roles import AgentRole
from patchfrog.review.domain import ReviewRunStatus
from patchfrog.review.effort_types import ReviewEffortTier
from patchfrog.review_memory.config import NO_MEMORY_CONTEXT_FINGERPRINT
from patchfrog.test_intelligence.telemetry import TestIntelligenceSummary


class ReviewRunRepository:
    """Persistence operations for :class:`ReviewRunModel`.

    Identity for idempotency/concurrency purposes is ``(repository_id,
    commit_sha, config_fingerprint, model_fingerprint,
    incremental_context_fingerprint)`` -- mirrors
    :class:`patchfrog.persistence.repositories.analysis_run.AnalysisRunRepository`
    exactly, including the transaction-scoped PostgreSQL advisory lock
    guarding creation/claim/success (no-op on SQLite). ``config_fingerprint``
    is configuration intent (:meth:`patchfrog.review.config.ReviewConfig.fingerprint`);
    ``model_fingerprint`` is the effective toolchain actually used
    (:meth:`patchfrog.review.config.ReviewModelIdentity.fingerprint` --
    reviewer/critic provider+model, prompt/policy/engine version).
    ``incremental_context_fingerprint`` (Phase 7) additionally
    distinguishes a FULL run from an INCREMENTAL one tied to a specific
    previous review generation -- see
    :func:`patchfrog.review_memory.config.compute_incremental_context_fingerprint`.
    All four/five must match for a prior run to be reused -- see the
    module docstring of :mod:`patchfrog.persistence.models.review` for
    why.
    """

    async def _lock_identity(
        self,
        session: AsyncSession,
        *,
        repository_id: uuid.UUID,
        commit_sha: str,
        config_fingerprint: str,
        model_fingerprint: str,
        incremental_context_fingerprint: str,
    ) -> None:
        if session.bind is None or session.bind.dialect.name != "postgresql":
            return
        key_material = (
            f"review:{repository_id}:{commit_sha}:{config_fingerprint}:"
            f"{model_fingerprint}:{incremental_context_fingerprint}"
        )
        digest = hashlib.sha256(key_material.encode()).digest()[:8]
        lock_key = int.from_bytes(digest, "big") & 0x7FFFFFFFFFFFFFFF
        await session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": lock_key})

    async def get_succeeded(
        self,
        session: AsyncSession,
        *,
        repository_id: uuid.UUID,
        commit_sha: str,
        config_fingerprint: str,
        model_fingerprint: str,
        incremental_context_fingerprint: str,
    ) -> ReviewRunModel | None:
        result = await session.execute(
            select(ReviewRunModel).where(
                ReviewRunModel.repository_id == repository_id,
                ReviewRunModel.commit_sha == commit_sha,
                ReviewRunModel.config_fingerprint == config_fingerprint,
                ReviewRunModel.model_fingerprint == model_fingerprint,
                ReviewRunModel.incremental_context_fingerprint == incremental_context_fingerprint,
                ReviewRunModel.status == ReviewRunStatus.SUCCEEDED,
            )
        )
        return result.scalar_one_or_none()

    async def claim_for_write(
        self,
        session: AsyncSession,
        *,
        run_id: uuid.UUID,
        repository_id: uuid.UUID,
        commit_sha: str,
        config_fingerprint: str,
        model_fingerprint: str,
        incremental_context_fingerprint: str,
    ) -> ReviewRunModel | None:
        """See ``AnalysisRunRepository.claim_for_write`` -- identical
        pattern. Must run before any candidate/proposal/finding rows are
        added to ``session`` for this run."""

        await self._lock_identity(
            session,
            repository_id=repository_id,
            commit_sha=commit_sha,
            config_fingerprint=config_fingerprint,
            model_fingerprint=model_fingerprint,
            incremental_context_fingerprint=incremental_context_fingerprint,
        )
        existing = await self.get_succeeded(
            session,
            repository_id=repository_id,
            commit_sha=commit_sha,
            config_fingerprint=config_fingerprint,
            model_fingerprint=model_fingerprint,
            incremental_context_fingerprint=incremental_context_fingerprint,
        )
        if existing is not None and existing.id != run_id:
            return existing
        return None

    async def get_or_create_running(
        self,
        session: AsyncSession,
        *,
        repository_id: uuid.UUID,
        repository_index_id: uuid.UUID,
        commit_sha: str,
        config_fingerprint: str,
        model_fingerprint: str,
        reviewer_provider: str,
        reviewer_model: str,
        critic_provider: str | None,
        critic_model: str | None,
        pull_request_id: uuid.UUID | None = None,
        analysis_run_id: uuid.UUID | None = None,
        incremental_context_fingerprint: str | None = None,
    ) -> tuple[ReviewRunModel, bool]:
        incremental_context_fingerprint = incremental_context_fingerprint or NO_MEMORY_CONTEXT_FINGERPRINT

        await self._lock_identity(
            session,
            repository_id=repository_id,
            commit_sha=commit_sha,
            config_fingerprint=config_fingerprint,
            model_fingerprint=model_fingerprint,
            incremental_context_fingerprint=incremental_context_fingerprint,
        )
        existing = await self.get_succeeded(
            session,
            repository_id=repository_id,
            commit_sha=commit_sha,
            config_fingerprint=config_fingerprint,
            model_fingerprint=model_fingerprint,
            incremental_context_fingerprint=incremental_context_fingerprint,
        )
        if existing is not None:
            return existing, False

        model = ReviewRunModel(
            repository_id=repository_id,
            repository_index_id=repository_index_id,
            analysis_run_id=analysis_run_id,
            pull_request_id=pull_request_id,
            commit_sha=commit_sha,
            config_fingerprint=config_fingerprint,
            model_fingerprint=model_fingerprint,
            incremental_context_fingerprint=incremental_context_fingerprint,
            status=ReviewRunStatus.RUNNING,
            reviewer_provider=reviewer_provider,
            reviewer_model=reviewer_model,
            critic_provider=critic_provider,
            critic_model=critic_model,
            started_at=datetime.now(UTC),
        )
        session.add(model)
        await session.flush()
        return model, True

    async def mark_succeeded(
        self,
        session: AsyncSession,
        *,
        run_id: uuid.UUID,
        status: ReviewRunStatus,
        candidate_count: int,
        candidates_reviewed: int,
        candidates_failed: int,
        candidates_skipped_budget: int,
        proposals_count: int,
        accepted_count: int,
        rejected_count: int,
        suppressed_duplicate_count: int,
        reviewer_input_tokens: int,
        reviewer_output_tokens: int,
        critic_input_tokens: int,
        critic_output_tokens: int,
        duration_ms: float,
        correctness_input_tokens: int = 0,
        correctness_output_tokens: int = 0,
        security_input_tokens: int = 0,
        security_output_tokens: int = 0,
        correctness_thinking_tokens: int = 0,
        security_thinking_tokens: int = 0,
        reviewer_thinking_tokens: int = 0,
        critic_thinking_tokens: int = 0,
        candidates_by_tier: dict[ReviewEffortTier, int] | None = None,
        candidates_escalated: int = 0,
        critic_calls: int = 0,
        retries_consumed: int = 0,
        reviewer_latency_ms: float = 0.0,
        calls_by_role: dict[AgentRole, int] | None = None,
        change_intelligence: ChangeIntelligenceSummary | None = None,
        contract_intelligence: ContractIntelligenceSummary | None = None,
        intent_verification: IntentVerificationSummary | None = None,
        test_intelligence: TestIntelligenceSummary | None = None,
        historical_regression_memory: HistoricalRegressionMemorySummary | None = None,
    ) -> ReviewRunModel:
        """Mark a run succeeded or partial. Returns the *canonical* run for
        this identity -- if a concurrent run already claimed
        ``status='succeeded'`` first, this run is marked ``failed`` as
        superseded and the winner is returned instead."""

        model = await session.get(ReviewRunModel, run_id)
        if model is None:
            raise ValueError(f"No review run with id {run_id}")

        await self._lock_identity(
            session,
            repository_id=model.repository_id,
            commit_sha=model.commit_sha,
            config_fingerprint=model.config_fingerprint,
            model_fingerprint=model.model_fingerprint,
            incremental_context_fingerprint=model.incremental_context_fingerprint,
        )
        existing = await self.get_succeeded(
            session,
            repository_id=model.repository_id,
            commit_sha=model.commit_sha,
            config_fingerprint=model.config_fingerprint,
            model_fingerprint=model.model_fingerprint,
            incremental_context_fingerprint=model.incremental_context_fingerprint,
        )
        if existing is not None and existing.id != model.id:
            model.status = ReviewRunStatus.FAILED
            model.error_message = f"superseded by concurrent review run {existing.id}"
            model.completed_at = datetime.now(UTC)
            await session.flush()
            return existing

        model.status = status
        model.candidate_count = candidate_count
        model.candidates_reviewed = candidates_reviewed
        model.candidates_failed = candidates_failed
        model.candidates_skipped_budget = candidates_skipped_budget
        model.proposals_count = proposals_count
        model.accepted_count = accepted_count
        model.rejected_count = rejected_count
        model.suppressed_duplicate_count = suppressed_duplicate_count
        model.reviewer_input_tokens = reviewer_input_tokens
        model.reviewer_output_tokens = reviewer_output_tokens
        model.critic_input_tokens = critic_input_tokens
        model.critic_output_tokens = critic_output_tokens
        model.correctness_input_tokens = correctness_input_tokens
        model.correctness_output_tokens = correctness_output_tokens
        model.security_input_tokens = security_input_tokens
        model.security_output_tokens = security_output_tokens
        model.correctness_thinking_tokens = correctness_thinking_tokens
        model.security_thinking_tokens = security_thinking_tokens
        model.reviewer_thinking_tokens = reviewer_thinking_tokens
        model.critic_thinking_tokens = critic_thinking_tokens
        model.candidates_by_tier = json.dumps({tier.value: count for tier, count in (candidates_by_tier or {}).items()})
        model.candidates_escalated = candidates_escalated
        model.critic_calls = critic_calls
        model.retries_consumed = retries_consumed
        model.reviewer_latency_ms = reviewer_latency_ms
        model.calls_by_role = json.dumps({role.value: count for role, count in (calls_by_role or {}).items()})
        model.duration_ms = duration_ms
        if change_intelligence is not None:
            model.change_unit_count = change_intelligence.change_unit_count
            model.change_kind_counts = change_intelligence.change_kind_counts_json
            model.affected_surface_count = change_intelligence.affected_surface_count
            model.expected_companion_count = change_intelligence.expected_companion_count
            model.missing_companion_candidate_count = change_intelligence.missing_companion_candidate_count
            model.change_map_rendered = change_intelligence.change_map_rendered
            model.change_map_node_count = change_intelligence.change_map_node_count
            model.change_story = change_intelligence.change_story
            model.change_map_text = change_intelligence.change_map_text
        if contract_intelligence is not None:
            model.contract_delta_count = contract_intelligence.contract_delta_count
            model.contract_kind_counts = contract_intelligence.contract_kind_counts_json
            model.potentially_breaking_delta_count = contract_intelligence.potentially_breaking_delta_count
            model.impacted_consumer_count = contract_intelligence.impacted_consumer_count
            model.stale_consumer_candidate_count = contract_intelligence.stale_consumer_candidate_count
        if intent_verification is not None:
            model.intent_claim_count = intent_verification.intent_claim_count
            model.intent_source_kind_counts = intent_verification.intent_source_kind_counts_json
            model.mapped_intent_claim_count = intent_verification.mapped_intent_claim_count
            model.intent_gap_candidate_count = intent_verification.intent_gap_candidate_count
            model.intent_coverage_summary_rendered = intent_verification.intent_coverage_summary_rendered
            model.intent_coverage_summary_text = intent_verification.intent_coverage_summary_text
        if test_intelligence is not None:
            model.test_expectation_count = test_intelligence.test_expectation_count
            model.test_reason_code_counts = test_intelligence.test_reason_code_counts_json
            model.test_gap_candidate_count = test_intelligence.test_gap_candidate_count
            model.test_coverage_summary_rendered = test_intelligence.test_coverage_summary_rendered
            model.test_coverage_summary_text = test_intelligence.test_coverage_summary_text
        if historical_regression_memory is not None:
            model.historical_trusted_record_count = historical_regression_memory.historical_trusted_record_count
            model.historical_match_kind_counts = historical_regression_memory.historical_match_kind_counts_json
            model.historical_regression_candidate_count = (
                historical_regression_memory.historical_regression_candidate_count
            )
            model.historical_summary_rendered = historical_regression_memory.historical_summary_rendered
            model.historical_summary_text = historical_regression_memory.historical_summary_text
        model.completed_at = datetime.now(UTC)
        await session.flush()
        return model

    async def mark_failed(
        self, session: AsyncSession, *, run_id: uuid.UUID, error_message: str
    ) -> ReviewRunModel:
        model = await session.get(ReviewRunModel, run_id)
        if model is None:
            raise ValueError(f"No review run with id {run_id}")

        model.status = ReviewRunStatus.FAILED
        model.error_message = error_message
        model.completed_at = datetime.now(UTC)
        await session.flush()
        return model

    async def get_by_id(self, session: AsyncSession, *, run_id: uuid.UUID) -> ReviewRunModel | None:
        return await session.get(ReviewRunModel, run_id)
