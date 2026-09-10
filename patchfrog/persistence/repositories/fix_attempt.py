from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from patchfrog.fix_verification.domain import ACTIVE_STATUSES, FixAttemptStatus
from patchfrog.persistence.models.fix_attempt import FixAttemptModel


class FixAttemptRepository:
    """Persistence operations for :class:`FixAttemptModel`. Identity for
    idempotency purposes is ``(handoff_id, candidate_fix_commit_sha)`` --
    see that model's own docstring."""

    async def get_existing(
        self, session: AsyncSession, *, handoff_id: str, candidate_fix_commit_sha: str
    ) -> FixAttemptModel | None:
        result = await session.execute(
            select(FixAttemptModel).where(
                FixAttemptModel.handoff_id == handoff_id,
                FixAttemptModel.candidate_fix_commit_sha == candidate_fix_commit_sha,
            )
        )
        return result.scalar_one_or_none()

    async def create(
        self,
        session: AsyncSession,
        *,
        handoff_id: str,
        finding_id: uuid.UUID,
        repository_id: uuid.UUID,
        original_commit_sha: str,
        candidate_fix_commit_sha: str,
    ) -> tuple[FixAttemptModel, bool]:
        """Returns ``(model, created)`` -- ``created=False`` when an
        identical ``(handoff_id, candidate_fix_commit_sha)`` attempt
        already exists (Part AF idempotency): the existing row is
        returned unchanged, never a duplicate."""

        existing = await self.get_existing(
            session, handoff_id=handoff_id, candidate_fix_commit_sha=candidate_fix_commit_sha
        )
        if existing is not None:
            return existing, False

        model = FixAttemptModel(
            handoff_id=handoff_id,
            finding_id=finding_id,
            repository_id=repository_id,
            original_commit_sha=original_commit_sha,
            candidate_fix_commit_sha=candidate_fix_commit_sha,
            status=FixAttemptStatus.PENDING,
        )
        session.add(model)
        try:
            await session.flush()
        except IntegrityError:
            # A concurrent request raced this one to the same identity --
            # the unique constraint caught it; the loser defers to the
            # winner rather than raising (same pattern as
            # ReviewRunRepository.mark_succeeded's re-lock-and-defer).
            await session.rollback()
            winner = await self.get_existing(
                session, handoff_id=handoff_id, candidate_fix_commit_sha=candidate_fix_commit_sha
            )
            if winner is None:
                raise
            return winner, False
        return model, True

    async def get_by_id(self, session: AsyncSession, *, fix_attempt_id: uuid.UUID) -> FixAttemptModel | None:
        return await session.get(FixAttemptModel, fix_attempt_id)

    async def count_active_for_repository(self, session: AsyncSession, *, repository_id: uuid.UUID) -> int:
        result = await session.execute(
            select(func.count())
            .select_from(FixAttemptModel)
            .where(
                FixAttemptModel.repository_id == repository_id,
                FixAttemptModel.status.in_([s.value for s in ACTIVE_STATUSES]),
            )
        )
        return int(result.scalar_one())

    async def count_for_finding(self, session: AsyncSession, *, finding_id: uuid.UUID) -> int:
        result = await session.execute(
            select(func.count()).select_from(FixAttemptModel).where(FixAttemptModel.finding_id == finding_id)
        )
        return int(result.scalar_one())

    async def list_for_finding(self, session: AsyncSession, *, finding_id: uuid.UUID) -> list[FixAttemptModel]:
        result = await session.execute(
            select(FixAttemptModel)
            .where(FixAttemptModel.finding_id == finding_id)
            .order_by(FixAttemptModel.created_at.desc())
        )
        return list(result.scalars().all())

    async def mark_verifying(self, session: AsyncSession, *, fix_attempt_id: uuid.UUID) -> FixAttemptModel:
        model = await session.get(FixAttemptModel, fix_attempt_id)
        if model is None:
            raise ValueError(f"No fix attempt with id {fix_attempt_id}")
        model.status = FixAttemptStatus.VERIFYING
        await session.flush()
        return model

    async def mark_result(
        self,
        session: AsyncSession,
        *,
        fix_attempt_id: uuid.UUID,
        status: FixAttemptStatus,
        deterministic_evidence: tuple[str, ...],
        executable_verification_outcome: str | None,
        remaining_issue_summary: str | None,
        limitations: tuple[str, ...],
    ) -> FixAttemptModel:
        model = await session.get(FixAttemptModel, fix_attempt_id)
        if model is None:
            raise ValueError(f"No fix attempt with id {fix_attempt_id}")
        model.status = status
        model.deterministic_evidence = json.dumps(list(deterministic_evidence))
        model.executable_verification_outcome = executable_verification_outcome
        model.remaining_issue_summary = remaining_issue_summary
        model.limitations = json.dumps(list(limitations))
        model.completed_at = datetime.now(UTC)
        await session.flush()
        return model

    async def mark_error(
        self, session: AsyncSession, *, fix_attempt_id: uuid.UUID, error_message: str
    ) -> FixAttemptModel:
        model = await session.get(FixAttemptModel, fix_attempt_id)
        if model is None:
            raise ValueError(f"No fix attempt with id {fix_attempt_id}")
        model.status = FixAttemptStatus.ERROR
        model.error_message = error_message
        model.completed_at = datetime.now(UTC)
        await session.flush()
        return model
