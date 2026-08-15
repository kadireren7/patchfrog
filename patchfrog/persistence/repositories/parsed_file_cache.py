from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from patchfrog.domain.code import Language
from patchfrog.persistence.models.parsed_file_cache import ParsedFileCacheModel


class ParsedFileCacheRepository:
    """Content-addressed cache of serialized parse results.

    Lookups never depend on repository or commit — only on
    ``(content_hash, language)`` — so a repeat run of the same file
    content anywhere skips Tree-sitter parsing entirely (see
    :mod:`patchfrog.indexing.parse_cache`).
    """

    async def get(
        self, session: AsyncSession, *, content_hash: str, language: Language
    ) -> str | None:
        result = await session.execute(
            select(ParsedFileCacheModel.payload).where(
                ParsedFileCacheModel.content_hash == content_hash,
                ParsedFileCacheModel.language == language,
            )
        )
        return result.scalar_one_or_none()

    async def put(
        self, session: AsyncSession, *, content_hash: str, language: Language, payload: str
    ) -> None:
        """Insert a cache entry, tolerating a concurrent insert of the same key."""

        dialect = session.bind.dialect.name if session.bind is not None else ""
        insert = pg_insert if dialect == "postgresql" else sqlite_insert
        stmt = insert(ParsedFileCacheModel).values(
            content_hash=content_hash, language=language, payload=payload
        )
        stmt = stmt.on_conflict_do_nothing(index_elements=["content_hash", "language"])
        await session.execute(stmt)
