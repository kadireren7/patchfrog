from __future__ import annotations

import uuid
from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from patchfrog.persistence.models.analysis import AnalyzerExecutionModel


class AnalyzerExecutionRepository:
    async def bulk_create(
        self, session: AsyncSession, models: Sequence[AnalyzerExecutionModel]
    ) -> None:
        session.add_all(models)
        await session.flush()

    async def list_for_run(
        self, session: AsyncSession, *, analysis_run_id: uuid.UUID
    ) -> list[AnalyzerExecutionModel]:
        result = await session.execute(
            select(AnalyzerExecutionModel).where(
                AnalyzerExecutionModel.analysis_run_id == analysis_run_id
            )
        )
        return list(result.scalars().all())
