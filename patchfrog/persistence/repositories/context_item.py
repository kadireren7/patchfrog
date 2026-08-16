from __future__ import annotations

import uuid
from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from patchfrog.context.domain import ContextItemKind
from patchfrog.persistence.models.context import ContextItemModel


class ContextItemRepository:
    """Persistence operations for :class:`ContextItemModel`."""

    async def bulk_create(self, session: AsyncSession, models: Sequence[ContextItemModel]) -> None:
        session.add_all(models)
        await session.flush()

    async def list_for_bundle(
        self, session: AsyncSession, *, bundle_id: uuid.UUID
    ) -> list[ContextItemModel]:
        result = await session.execute(
            select(ContextItemModel).where(ContextItemModel.bundle_id == bundle_id).order_by(ContextItemModel.rank)
        )
        return list(result.scalars().all())

    async def list_for_bundle_by_kind(
        self, session: AsyncSession, *, bundle_id: uuid.UUID, kind: ContextItemKind
    ) -> list[ContextItemModel]:
        result = await session.execute(
            select(ContextItemModel)
            .where(ContextItemModel.bundle_id == bundle_id, ContextItemModel.kind == kind)
            .order_by(ContextItemModel.rank)
        )
        return list(result.scalars().all())
