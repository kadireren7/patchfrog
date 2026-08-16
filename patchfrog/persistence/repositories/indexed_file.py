from __future__ import annotations

import uuid
from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from patchfrog.domain.code import Language
from patchfrog.persistence.models.code_index import IndexedFileModel


class IndexedFileRepository:
    """Persistence operations for :class:`IndexedFileModel`."""

    async def bulk_create(self, session: AsyncSession, models: Sequence[IndexedFileModel]) -> None:
        session.add_all(models)
        await session.flush()

    async def list_for_index(
        self, session: AsyncSession, *, repository_index_id: uuid.UUID
    ) -> list[IndexedFileModel]:
        result = await session.execute(
            select(IndexedFileModel).where(IndexedFileModel.repository_index_id == repository_index_id)
        )
        return list(result.scalars().all())

    async def get_by_path(
        self, session: AsyncSession, *, repository_index_id: uuid.UUID, relative_path: str
    ) -> IndexedFileModel | None:
        result = await session.execute(
            select(IndexedFileModel).where(
                IndexedFileModel.repository_index_id == repository_index_id,
                IndexedFileModel.relative_path == relative_path,
            )
        )
        return result.scalar_one_or_none()

    async def get_by_id(self, session: AsyncSession, *, indexed_file_id: uuid.UUID) -> IndexedFileModel | None:
        return await session.get(IndexedFileModel, indexed_file_id)

    async def distinct_languages(
        self, session: AsyncSession, *, repository_index_id: uuid.UUID
    ) -> frozenset[Language]:
        result = await session.execute(
            select(IndexedFileModel.language)
            .where(
                IndexedFileModel.repository_index_id == repository_index_id,
                IndexedFileModel.language.is_not(None),
            )
            .distinct()
        )
        return frozenset(lang for lang in result.scalars().all() if lang is not None)
