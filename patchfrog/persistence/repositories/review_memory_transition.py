from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from patchfrog.persistence.models.review_memory import ReviewMemoryTransitionModel


class ReviewMemoryTransitionRepository:
    """Read-only queries over the append-only transition audit trail --
    all writes happen via :class:`~patchfrog.persistence.repositories.review_memory_finding.ReviewMemoryFindingRepository`,
    which always writes exactly one transition alongside every status
    change."""

    async def list_for_finding(
        self, session: AsyncSession, *, memory_finding_id: uuid.UUID
    ) -> list[ReviewMemoryTransitionModel]:
        result = await session.execute(
            select(ReviewMemoryTransitionModel)
            .where(ReviewMemoryTransitionModel.memory_finding_id == memory_finding_id)
            .order_by(ReviewMemoryTransitionModel.created_at.asc())
        )
        return list(result.scalars().all())

    async def list_for_target_run(
        self, session: AsyncSession, *, target_review_run_id: uuid.UUID
    ) -> list[ReviewMemoryTransitionModel]:
        result = await session.execute(
            select(ReviewMemoryTransitionModel).where(
                ReviewMemoryTransitionModel.target_review_run_id == target_review_run_id
            )
        )
        return list(result.scalars().all())
