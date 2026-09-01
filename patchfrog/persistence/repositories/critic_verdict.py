from __future__ import annotations

import uuid
from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from patchfrog.persistence.models.review import CriticVerdictModel
from patchfrog.review.domain import CriticVerdict


class CriticVerdictRepository:
    async def create(
        self, session: AsyncSession, *, proposal_id: uuid.UUID, verdict: CriticVerdict
    ) -> CriticVerdictModel:
        model = CriticVerdictModel(
            proposal_id=proposal_id,
            decision=verdict.decision,
            reasoning_summary=verdict.reasoning_summary,
            downgraded_severity=verdict.downgraded_severity,
            downgraded_confidence=verdict.downgraded_confidence,
            provider=verdict.provider,
            model=verdict.model,
            input_tokens=verdict.input_tokens,
            output_tokens=verdict.output_tokens,
            thinking_tokens=verdict.thinking_tokens,
            latency_ms=verdict.latency_ms,
        )
        session.add(model)
        await session.flush()
        return model

    async def get_for_proposal(
        self, session: AsyncSession, *, proposal_id: uuid.UUID
    ) -> CriticVerdictModel | None:
        result = await session.execute(
            select(CriticVerdictModel).where(CriticVerdictModel.proposal_id == proposal_id)
        )
        return result.scalar_one_or_none()

    async def list_for_proposal_ids(
        self, session: AsyncSession, *, proposal_ids: Sequence[uuid.UUID]
    ) -> list[CriticVerdictModel]:
        """One query for every verdict across many proposals -- the bulk
        counterpart to :meth:`get_for_proposal`, used by
        :mod:`patchfrog.telemetry.collector` so collecting one review
        run's telemetry never issues one verdict query per proposal (spec
        section 43: avoid N+1)."""

        if not proposal_ids:
            return []
        result = await session.execute(
            select(CriticVerdictModel).where(CriticVerdictModel.proposal_id.in_(proposal_ids))
        )
        return list(result.scalars().all())
