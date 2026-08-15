from __future__ import annotations

import uuid
from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from patchfrog.persistence.models.code_index import ImportReferenceModel


class ImportReferenceRepository:
    """Persistence operations for :class:`ImportReferenceModel`."""

    async def bulk_create(
        self, session: AsyncSession, models: Sequence[ImportReferenceModel]
    ) -> None:
        session.add_all(models)
        await session.flush()

    async def imports_from_file(
        self, session: AsyncSession, *, indexed_file_id: uuid.UUID
    ) -> list[ImportReferenceModel]:
        result = await session.execute(
            select(ImportReferenceModel).where(ImportReferenceModel.indexed_file_id == indexed_file_id)
        )
        return list(result.scalars().all())

    async def files_importing_file(
        self, session: AsyncSession, *, resolved_file_id: uuid.UUID
    ) -> list[ImportReferenceModel]:
        result = await session.execute(
            select(ImportReferenceModel).where(ImportReferenceModel.resolved_file_id == resolved_file_id)
        )
        return list(result.scalars().all())
