from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from patchfrog.persistence.models.pull_request_ingestion import (
    IngestionStatus,
    PullRequestIngestionModel,
)


class PullRequestIngestionRepository:
    """Persistence operations for :class:`PullRequestIngestionModel`.

    ``reserve`` is the idempotency guard: it relies on the unique
    constraint on ``delivery_id`` to guarantee that concurrent or retried
    processing of the same GitHub webhook delivery only ever reserves the
    row once.
    """

    async def reserve(
        self, session: AsyncSession, *, delivery_id: str, event_action: str, head_sha: str
    ) -> PullRequestIngestionModel | None:
        """Attempt to claim ``delivery_id`` for processing.

        Returns the newly created row, or ``None`` if this delivery has
        already been reserved (by this call or a previous one) — the
        caller should treat that as a no-op duplicate.
        """

        model = PullRequestIngestionModel(
            delivery_id=delivery_id,
            event_action=event_action,
            head_sha=head_sha,
            status=IngestionStatus.IN_PROGRESS,
            started_at=datetime.now(UTC),
        )
        session.add(model)
        try:
            await session.flush()
        except IntegrityError:
            await session.rollback()
            return None
        return model

    async def get_by_delivery_id(
        self, session: AsyncSession, *, delivery_id: str
    ) -> PullRequestIngestionModel | None:
        result = await session.execute(
            select(PullRequestIngestionModel).where(
                PullRequestIngestionModel.delivery_id == delivery_id
            )
        )
        return result.scalar_one_or_none()

    async def mark_succeeded(
        self,
        session: AsyncSession,
        *,
        ingestion_id: uuid.UUID,
        pull_request_id: uuid.UUID,
    ) -> None:
        ingestion = await session.get_one(PullRequestIngestionModel, ingestion_id)
        ingestion.status = IngestionStatus.SUCCEEDED
        ingestion.pull_request_id = pull_request_id
        ingestion.completed_at = datetime.now(UTC)
        await session.flush()

    async def mark_failed(
        self, session: AsyncSession, *, ingestion_id: uuid.UUID, error_message: str
    ) -> None:
        ingestion = await session.get_one(PullRequestIngestionModel, ingestion_id)
        ingestion.status = IngestionStatus.FAILED
        ingestion.error_message = error_message[:2000]
        ingestion.completed_at = datetime.now(UTC)
        await session.flush()
