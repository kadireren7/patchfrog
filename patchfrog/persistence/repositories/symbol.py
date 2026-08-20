from __future__ import annotations

import uuid
from collections.abc import Sequence

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from patchfrog.persistence.models.code_index import SymbolModel


class SymbolRepository:
    """Persistence operations for :class:`SymbolModel`."""

    async def bulk_create(self, session: AsyncSession, models: Sequence[SymbolModel]) -> None:
        session.add_all(models)
        await session.flush()

    async def symbols_in_file(
        self, session: AsyncSession, *, indexed_file_id: uuid.UUID
    ) -> list[SymbolModel]:
        result = await session.execute(
            select(SymbolModel)
            .where(SymbolModel.indexed_file_id == indexed_file_id)
            .order_by(SymbolModel.start_line)
        )
        return list(result.scalars().all())

    async def list_for_index(
        self, session: AsyncSession, *, repository_index_id: uuid.UUID
    ) -> list[SymbolModel]:
        """Every symbol for one index version, one query -- used by
        :mod:`patchfrog.review_memory.symbol_continuity` to compare two
        whole index versions without an N+1 per-symbol lookup."""

        result = await session.execute(
            select(SymbolModel).where(SymbolModel.repository_index_id == repository_index_id)
        )
        return list(result.scalars().all())

    async def find_by_name(
        self, session: AsyncSession, *, repository_index_id: uuid.UUID, name: str
    ) -> list[SymbolModel]:
        result = await session.execute(
            select(SymbolModel).where(
                SymbolModel.repository_index_id == repository_index_id,
                SymbolModel.name == name,
            )
        )
        return list(result.scalars().all())

    async def find_by_qualified_name(
        self, session: AsyncSession, *, repository_index_id: uuid.UUID, qualified_name: str
    ) -> list[SymbolModel]:
        result = await session.execute(
            select(SymbolModel).where(
                SymbolModel.repository_index_id == repository_index_id,
                SymbolModel.qualified_name == qualified_name,
            )
        )
        return list(result.scalars().all())

    async def symbol_containing_line(
        self, session: AsyncSession, *, indexed_file_id: uuid.UUID, line: int
    ) -> SymbolModel | None:
        """The innermost symbol whose span contains ``line``, if any.

        "Innermost" is approximated as the candidate with the smallest
        line span — correct as long as symbol spans are properly nested,
        which every language parser here guarantees (a child symbol's
        span is always produced from a descendant Tree-sitter node).
        """

        result = await session.execute(
            select(SymbolModel)
            .where(
                SymbolModel.indexed_file_id == indexed_file_id,
                and_(SymbolModel.start_line <= line, SymbolModel.end_line >= line),
            )
            .order_by((SymbolModel.end_line - SymbolModel.start_line).asc())
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def get_by_id(self, session: AsyncSession, *, symbol_id: uuid.UUID) -> SymbolModel | None:
        return await session.get(SymbolModel, symbol_id)

    async def get_many_by_ids(
        self, session: AsyncSession, *, symbol_ids: Sequence[uuid.UUID]
    ) -> dict[uuid.UUID, SymbolModel]:
        """Batched form of :meth:`get_by_id` -- one query for a whole set
        of ids rather than one round-trip per id, for candidate-generation
        callers resolving many callers/callees at once."""

        if not symbol_ids:
            return {}
        result = await session.execute(select(SymbolModel).where(SymbolModel.id.in_(symbol_ids)))
        return {model.id: model for model in result.scalars().all()}
