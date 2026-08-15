"""Analysis query APIs.

Read-only queries over persisted analysis runs and findings — the layer
future review/presentation phases will depend on. Every query is scoped
by an explicit ``analysis_run_id`` (or a value derived from one, like a
``symbol_id``/``finding_id``) — there is deliberately no query that spans
multiple runs implicitly, so results can never mix findings from two
different analysis runs of the same repository.
"""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from patchfrog.analysis.domain import Severity
from patchfrog.persistence.models.analysis import (
    AnalysisRunModel,
    AnalyzerExecutionModel,
    FindingModel,
    FindingSourceModel,
)
from patchfrog.persistence.repositories import (
    AnalysisRunRepository,
    AnalyzerExecutionRepository,
    FindingRepository,
    FindingSourceRepository,
)


class AnalysisQueryService:
    def __init__(self) -> None:
        self._run_repo = AnalysisRunRepository()
        self._execution_repo = AnalyzerExecutionRepository()
        self._finding_repo = FindingRepository()
        self._source_repo = FindingSourceRepository()

    async def get_run(self, session: AsyncSession, *, run_id: uuid.UUID) -> AnalysisRunModel | None:
        return await self._run_repo.get_by_id(session, run_id=run_id)

    async def get_succeeded_run(
        self,
        session: AsyncSession,
        *,
        repository_id: uuid.UUID,
        commit_sha: str,
        config_fingerprint: str,
        toolchain_fingerprint: str,
    ) -> AnalysisRunModel | None:
        return await self._run_repo.get_succeeded(
            session,
            repository_id=repository_id,
            commit_sha=commit_sha,
            config_fingerprint=config_fingerprint,
            toolchain_fingerprint=toolchain_fingerprint,
        )

    async def get_findings_for_run(
        self, session: AsyncSession, *, analysis_run_id: uuid.UUID
    ) -> list[FindingModel]:
        return await self._finding_repo.list_for_run(session, analysis_run_id=analysis_run_id)

    async def get_findings_for_file(
        self, session: AsyncSession, *, analysis_run_id: uuid.UUID, file_path: str
    ) -> list[FindingModel]:
        return await self._finding_repo.list_for_file(
            session, analysis_run_id=analysis_run_id, file_path=file_path
        )

    async def get_findings_for_symbol(
        self, session: AsyncSession, *, symbol_id: uuid.UUID
    ) -> list[FindingModel]:
        return await self._finding_repo.list_for_symbol(session, symbol_id=symbol_id)

    async def get_findings_on_changed_lines(
        self, session: AsyncSession, *, analysis_run_id: uuid.UUID
    ) -> list[FindingModel]:
        return await self._finding_repo.list_on_changed_lines(session, analysis_run_id=analysis_run_id)

    async def get_findings_in_changed_symbols(
        self, session: AsyncSession, *, analysis_run_id: uuid.UUID
    ) -> list[FindingModel]:
        return await self._finding_repo.list_in_changed_symbols(session, analysis_run_id=analysis_run_id)

    async def get_findings_by_severity(
        self, session: AsyncSession, *, analysis_run_id: uuid.UUID, severity: Severity
    ) -> list[FindingModel]:
        return await self._finding_repo.list_by_severity(
            session, analysis_run_id=analysis_run_id, severity=severity
        )

    async def get_findings_by_analyzer(
        self, session: AsyncSession, *, analysis_run_id: uuid.UUID, analyzer: str
    ) -> list[FindingModel]:
        return await self._finding_repo.list_by_analyzer(
            session, analysis_run_id=analysis_run_id, analyzer=analyzer
        )

    async def get_finding_sources(
        self, session: AsyncSession, *, finding_id: uuid.UUID
    ) -> list[FindingSourceModel]:
        return await self._source_repo.list_for_finding(session, finding_id=finding_id)

    async def get_analyzer_executions(
        self, session: AsyncSession, *, analysis_run_id: uuid.UUID
    ) -> list[AnalyzerExecutionModel]:
        return await self._execution_repo.list_for_run(session, analysis_run_id=analysis_run_id)
