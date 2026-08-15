from __future__ import annotations

import uuid
from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from patchfrog.persistence.models.code_index import CallReferenceModel


class CallReferenceRepository:
    """Persistence operations for :class:`CallReferenceModel`."""

    async def bulk_create(self, session: AsyncSession, models: Sequence[CallReferenceModel]) -> None:
        session.add_all(models)
        await session.flush()

    async def callers_of_symbol(
        self, session: AsyncSession, *, symbol_id: uuid.UUID
    ) -> list[CallReferenceModel]:
        """Call sites that resolve *to* ``symbol_id`` — i.e. its callers."""

        result = await session.execute(
            select(CallReferenceModel).where(CallReferenceModel.resolved_symbol_id == symbol_id)
        )
        return list(result.scalars().all())

    async def callees_of_symbol(
        self, session: AsyncSession, *, symbol_id: uuid.UUID
    ) -> list[CallReferenceModel]:
        """Call sites made *from within* ``symbol_id`` — i.e. what it calls."""

        result = await session.execute(
            select(CallReferenceModel).where(CallReferenceModel.caller_symbol_id == symbol_id)
        )
        return list(result.scalars().all())
