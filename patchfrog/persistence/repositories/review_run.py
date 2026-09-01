from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from patchfrog.persistence.models.review import ReviewRunModel
from patchfrog.review.agents.roles import AgentRole
from patchfrog.review.domain import ReviewRunStatus
from patchfrog.review.effort_types import ReviewEffortTier
from patchfrog.review_memory.config import NO_MEMORY_CONTEXT_FINGERPRINT


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
