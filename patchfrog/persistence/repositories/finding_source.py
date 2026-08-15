from __future__ import annotations

import uuid
from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from patchfrog.persistence.models.analysis import FindingSourceModel


class FindingSourceRepository:
    async def bulk_create(
        self, session: AsyncSession, models: Sequence[FindingSourceModel]
    ) -> None:
        session.add_all(models)
        await session.flush()

    async def list_for_finding(
        self, session: AsyncSession, *, finding_id: uuid.UUID
    ) -> list[FindingSourceModel]:
        result = await session.execute(
            select(FindingSourceModel).where(FindingSourceModel.finding_id == finding_id)
        )
        return list(result.scalars().all())
