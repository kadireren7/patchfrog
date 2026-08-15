from __future__ import annotations

import uuid
from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from patchfrog.intelligence.graph import EdgeKind
from patchfrog.persistence.models.code_index import RepositoryEdgeModel


class RepositoryEdgeRepository:
    """Persistence operations for :class:`RepositoryEdgeModel`."""

    async def bulk_create(self, session: AsyncSession, models: Sequence[RepositoryEdgeModel]) -> None:
        session.add_all(models)
        await session.flush()

    async def edges_of_kind(
        self, session: AsyncSession, *, repository_index_id: uuid.UUID, kind: EdgeKind
    ) -> list[RepositoryEdgeModel]:
        result = await session.execute(
            select(RepositoryEdgeModel).where(
                RepositoryEdgeModel.repository_index_id == repository_index_id,
                RepositoryEdgeModel.kind == kind,
            )
        )
        return list(result.scalars().all())

    async def likely_tests_for_file(
        self, session: AsyncSession, *, source_file_id: uuid.UUID
    ) -> list[RepositoryEdgeModel]:
        """``FILE_TESTS_FILE`` edges targeting ``source_file_id`` — its likely tests."""

        result = await session.execute(
            select(RepositoryEdgeModel).where(
                RepositoryEdgeModel.kind == EdgeKind.FILE_TESTS_FILE,
                RepositoryEdgeModel.target_file_id == source_file_id,
            )
        )
        return list(result.scalars().all())
