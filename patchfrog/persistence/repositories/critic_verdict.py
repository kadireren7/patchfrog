from __future__ import annotations

import uuid

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
