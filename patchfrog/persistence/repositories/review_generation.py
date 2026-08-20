from __future__ import annotations

import uuid

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from patchfrog.persistence.models.review_memory import ReviewGenerationModel
from patchfrog.review_memory.domain import IncrementalRunMode, TransitionReasonCode


class ReviewGenerationRepository:
    """Persistence operations for :class:`ReviewGenerationModel`.

    ``get_latest_for_pr`` is the *only* place "most recent generation for
    this PR" is ever decided -- called once, when a new review is about
    to start, under proof of ancestry (see
    :mod:`patchfrog.review_memory.service`). Every other query in this
    module follows an already-persisted ``previous_generation_id`` link
    explicitly, never re-deriving "nearest" anything (see the Phase 7
    spec's "PR Sequence Safety"). Ordered by ``sequence_number``, never
    ``created_at`` -- two generations for the same PR can be created
    within the same timestamp tick, and ``ORDER BY created_at`` alone
    would then non-deterministically pick either one (see
    :class:`~patchfrog.persistence.models.review_memory.ReviewGenerationModel`'s
    ``sequence_number`` docstring).
    """

    async def _lock_pull_request(self, session: AsyncSession, *, pull_request_id: uuid.UUID) -> None:
        if session.bind is None or session.bind.dialect.name != "postgresql":
            return
        lock_key = pull_request_id.int & 0x7FFFFFFFFFFFFFFF
        await session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": lock_key})

    async def create(
        self,
        session: AsyncSession,
        *,
        repository_id: uuid.UUID,
        pull_request_id: uuid.UUID,
        review_run_id: uuid.UUID,
        commit_sha: str,
        previous_generation_id: uuid.UUID | None,
        previous_commit_sha: str | None,
        ancestry_verified: bool,
        mode: IncrementalRunMode,
        compatibility_ok: bool,
        invalidation_reason: TransitionReasonCode | None,
        memory_compatibility_fingerprint: str,
    ) -> ReviewGenerationModel:
        await self._lock_pull_request(session, pull_request_id=pull_request_id)
        result = await session.execute(
            select(func.coalesce(func.max(ReviewGenerationModel.sequence_number), 0)).where(
                ReviewGenerationModel.pull_request_id == pull_request_id
            )
        )
        next_sequence_number = result.scalar_one() + 1

        model = ReviewGenerationModel(
            repository_id=repository_id,
            pull_request_id=pull_request_id,
            sequence_number=next_sequence_number,
            review_run_id=review_run_id,
            commit_sha=commit_sha,
            previous_generation_id=previous_generation_id,
            previous_commit_sha=previous_commit_sha,
            ancestry_verified=ancestry_verified,
            mode=mode,
            compatibility_ok=compatibility_ok,
            invalidation_reason=invalidation_reason,
            memory_compatibility_fingerprint=memory_compatibility_fingerprint,
        )
        session.add(model)
        await session.flush()
        return model

    async def get_latest_for_pr(
        self, session: AsyncSession, *, pull_request_id: uuid.UUID
    ) -> ReviewGenerationModel | None:
        """The highest-``sequence_number`` generation for this PR -- the
        ONE place "most recent" is used, and only to pick the candidate
        whose ancestry is then explicitly verified before it is trusted
        (see the module docstring)."""

        result = await session.execute(
            select(ReviewGenerationModel)
            .where(ReviewGenerationModel.pull_request_id == pull_request_id)
            .order_by(ReviewGenerationModel.sequence_number.desc())
            .limit(1)
        )
        return result.scalars().first()

    async def get_by_review_run_id(
        self, session: AsyncSession, *, review_run_id: uuid.UUID
    ) -> ReviewGenerationModel | None:
        result = await session.execute(
            select(ReviewGenerationModel).where(ReviewGenerationModel.review_run_id == review_run_id)
        )
        return result.scalar_one_or_none()

    async def get_by_id(
        self, session: AsyncSession, *, generation_id: uuid.UUID
    ) -> ReviewGenerationModel | None:
        return await session.get(ReviewGenerationModel, generation_id)

    async def list_for_pr(
        self, session: AsyncSession, *, pull_request_id: uuid.UUID
    ) -> list[ReviewGenerationModel]:
        result = await session.execute(
            select(ReviewGenerationModel)
            .where(ReviewGenerationModel.pull_request_id == pull_request_id)
            .order_by(ReviewGenerationModel.created_at.asc())
        )
        return list(result.scalars().all())
