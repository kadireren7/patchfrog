"""Repository intelligence query APIs.

Reads a persisted :class:`~patchfrog.persistence.models.repository_index.RepositoryIndexModel`
without touching Tree-sitter or re-parsing anything — every answer here
comes straight out of the tables :mod:`patchfrog.indexing.service` wrote.
Every "list" method returns an empty list rather than raising when
nothing matches; only a missing *index* itself is treated as an error
condition by the caller (there's nothing to query).
"""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from patchfrog.persistence.models.code_index import (
    CallReferenceModel,
    ImportReferenceModel,
    IndexedFileModel,
    RepositoryEdgeModel,
    SymbolModel,
)
from patchfrog.persistence.models.repository_index import RepositoryIndexModel
from patchfrog.persistence.repositories import (
    CallReferenceRepository,
    ImportReferenceRepository,
    IndexedFileRepository,
    RepositoryEdgeRepository,
    RepositoryIndexRepository,
    SymbolRepository,
)


class RepositoryQueryService:
    """Read-only query API over one repository's active (or given) index."""

    def __init__(self) -> None:
        self._index_repo = RepositoryIndexRepository()
        self._file_repo = IndexedFileRepository()
        self._symbol_repo = SymbolRepository()
        self._import_repo = ImportReferenceRepository()
        self._call_repo = CallReferenceRepository()
        self._edge_repo = RepositoryEdgeRepository()

    async def get_active_index(
        self, session: AsyncSession, *, repository_id: uuid.UUID
    ) -> RepositoryIndexModel | None:
        return await self._index_repo.get_active(session, repository_id=repository_id)

    async def get_file(
        self, session: AsyncSession, *, repository_index_id: uuid.UUID, relative_path: str
    ) -> IndexedFileModel | None:
        return await self._file_repo.get_by_path(
            session, repository_index_id=repository_index_id, relative_path=relative_path
        )

    async def get_file_by_id(
        self, session: AsyncSession, *, indexed_file_id: uuid.UUID
    ) -> IndexedFileModel | None:
        return await self._file_repo.get_by_id(session, indexed_file_id=indexed_file_id)

    async def get_symbol_by_id(
        self, session: AsyncSession, *, symbol_id: uuid.UUID
    ) -> SymbolModel | None:
        return await self._symbol_repo.get_by_id(session, symbol_id=symbol_id)

    async def symbols_in_file(
        self, session: AsyncSession, *, indexed_file_id: uuid.UUID
    ) -> list[SymbolModel]:
        return await self._symbol_repo.symbols_in_file(session, indexed_file_id=indexed_file_id)

    async def find_symbol_by_name(
        self, session: AsyncSession, *, repository_index_id: uuid.UUID, name: str
    ) -> list[SymbolModel]:
        return await self._symbol_repo.find_by_name(
            session, repository_index_id=repository_index_id, name=name
        )

    async def find_symbol_by_qualified_name(
        self, session: AsyncSession, *, repository_index_id: uuid.UUID, qualified_name: str
    ) -> list[SymbolModel]:
        return await self._symbol_repo.find_by_qualified_name(
            session, repository_index_id=repository_index_id, qualified_name=qualified_name
        )

    async def symbol_containing_line(
        self, session: AsyncSession, *, indexed_file_id: uuid.UUID, line: int
    ) -> SymbolModel | None:
        """The innermost symbol at ``indexed_file_id:line``, e.g. mapping a
        changed diff line to the function/method that contains it."""

        return await self._symbol_repo.symbol_containing_line(
            session, indexed_file_id=indexed_file_id, line=line
        )

    async def get_callers(
        self, session: AsyncSession, *, symbol_id: uuid.UUID
    ) -> list[CallReferenceModel]:
        return await self._call_repo.callers_of_symbol(session, symbol_id=symbol_id)

    async def get_callees(
        self, session: AsyncSession, *, symbol_id: uuid.UUID
    ) -> list[CallReferenceModel]:
        return await self._call_repo.callees_of_symbol(session, symbol_id=symbol_id)

    async def imports_from_file(
        self, session: AsyncSession, *, indexed_file_id: uuid.UUID
    ) -> list[ImportReferenceModel]:
        return await self._import_repo.imports_from_file(session, indexed_file_id=indexed_file_id)

    async def files_importing(
        self, session: AsyncSession, *, indexed_file_id: uuid.UUID
    ) -> list[ImportReferenceModel]:
        return await self._import_repo.files_importing_file(
            session, resolved_file_id=indexed_file_id
        )

    async def likely_tests_for_file(
        self, session: AsyncSession, *, indexed_file_id: uuid.UUID
    ) -> list[RepositoryEdgeModel]:
        return await self._edge_repo.likely_tests_for_file(session, source_file_id=indexed_file_id)

    async def symbol_for_changed_line(
        self, session: AsyncSession, *, repository_index_id: uuid.UUID, relative_path: str, line: int
    ) -> SymbolModel | None:
        """Map a PR-changed ``(file path, line number)`` to its containing symbol.

        Returns ``None`` when the file isn't indexed (unsupported
        language, not tracked, ...) or no symbol's span covers the line
        (e.g. it falls in module-level code) — never a guess.
        """

        file = await self.get_file(
            session, repository_index_id=repository_index_id, relative_path=relative_path
        )
        if file is None:
            return None
        return await self.symbol_containing_line(session, indexed_file_id=file.id, line=line)
