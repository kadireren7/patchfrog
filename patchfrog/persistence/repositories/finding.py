from __future__ import annotations

import uuid
from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from patchfrog.analysis.domain import Severity
from patchfrog.persistence.models.analysis import FindingModel


class FindingRepository:
    async def bulk_create(self, session: AsyncSession, models: Sequence[FindingModel]) -> None:
        session.add_all(models)
        await session.flush()

    async def list_for_run(
        self, session: AsyncSession, *, analysis_run_id: uuid.UUID
    ) -> list[FindingModel]:
        result = await session.execute(
            select(FindingModel).where(FindingModel.analysis_run_id == analysis_run_id)
        )
        return list(result.scalars().all())

    async def list_for_file(
        self, session: AsyncSession, *, analysis_run_id: uuid.UUID, file_path: str
    ) -> list[FindingModel]:
        result = await session.execute(
            select(FindingModel).where(
                FindingModel.analysis_run_id == analysis_run_id, FindingModel.file_path == file_path
            )
        )
        return list(result.scalars().all())

    async def list_for_symbol(
        self, session: AsyncSession, *, symbol_id: uuid.UUID
    ) -> list[FindingModel]:
        result = await session.execute(select(FindingModel).where(FindingModel.symbol_id == symbol_id))
        return list(result.scalars().all())

    async def list_by_severity(
        self, session: AsyncSession, *, analysis_run_id: uuid.UUID, severity: Severity
    ) -> list[FindingModel]:
        result = await session.execute(
            select(FindingModel).where(
                FindingModel.analysis_run_id == analysis_run_id, FindingModel.severity == severity
            )
        )
        return list(result.scalars().all())

    async def list_by_analyzer(
        self, session: AsyncSession, *, analysis_run_id: uuid.UUID, analyzer: str
    ) -> list[FindingModel]:
        result = await session.execute(
            select(FindingModel).where(
                FindingModel.analysis_run_id == analysis_run_id, FindingModel.source_analyzer == analyzer
            )
        )
        return list(result.scalars().all())

    async def list_on_changed_lines(
        self, session: AsyncSession, *, analysis_run_id: uuid.UUID
    ) -> list[FindingModel]:
        result = await session.execute(
            select(FindingModel).where(
                FindingModel.analysis_run_id == analysis_run_id,
                FindingModel.is_on_changed_line.is_(True),
            )
        )
        return list(result.scalars().all())

    async def list_in_changed_symbols(
        self, session: AsyncSession, *, analysis_run_id: uuid.UUID
    ) -> list[FindingModel]:
        result = await session.execute(
            select(FindingModel).where(
                FindingModel.analysis_run_id == analysis_run_id,
                FindingModel.is_in_changed_symbol.is_(True),
            )
        )
        return list(result.scalars().all())
