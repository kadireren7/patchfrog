from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import select, tuple_
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from patchfrog.domain.code import Language
from patchfrog.persistence.models.parsed_file_cache import ParsedFileCacheModel

CacheKey = tuple[str, Language, int]
"""``(content_hash, language, parser_version)``."""


class ParsedFileCacheRepository:
    """Content-addressed cache of serialized parse results.

    Lookups never depend on repository or commit — only on
    ``(content_hash, language, parser_version)`` — so a repeat run of the
    same file content anywhere, under the same parser version, skips
    Tree-sitter parsing entirely (see :mod:`patchfrog.indexing.parse_cache`).
    """

    async def get(
        self, session: AsyncSession, *, content_hash: str, language: Language, parser_version: int
    ) -> str | None:
        result = await session.execute(
            select(ParsedFileCacheModel.payload).where(
                ParsedFileCacheModel.content_hash == content_hash,
                ParsedFileCacheModel.language == language,
                ParsedFileCacheModel.parser_version == parser_version,
            )
        )
        return result.scalar_one_or_none()

    async def get_many(
        self, session: AsyncSession, *, keys: Sequence[CacheKey]
    ) -> dict[CacheKey, str]:
        """Look up every key in one round trip instead of one query per file.

        Indexing a repository with hundreds/thousands of unchanged files
        previously issued one ``SELECT`` per file just to find a cache hit
        — the dominant cost of an otherwise-cheap incremental run. This
        collapses that into a single composite-key ``IN`` query.
        """

        if not keys:
            return {}
        result = await session.execute(
            select(
                ParsedFileCacheModel.content_hash,
                ParsedFileCacheModel.language,
                ParsedFileCacheModel.parser_version,
                ParsedFileCacheModel.payload,
            ).where(
                tuple_(
                    ParsedFileCacheModel.content_hash,
                    ParsedFileCacheModel.language,
                    ParsedFileCacheModel.parser_version,
                ).in_(keys)
            )
        )
        return {(content_hash, language, version): payload for content_hash, language, version, payload in result.all()}

    async def put(
        self,
        session: AsyncSession,
        *,
        content_hash: str,
        language: Language,
        parser_version: int,
        payload: str,
    ) -> None:
        """Insert a cache entry, tolerating a concurrent insert of the same key."""

        dialect = session.bind.dialect.name if session.bind is not None else ""
        insert = pg_insert if dialect == "postgresql" else sqlite_insert
        stmt = insert(ParsedFileCacheModel).values(
            content_hash=content_hash, language=language, parser_version=parser_version, payload=payload
        )
        stmt = stmt.on_conflict_do_nothing(index_elements=["content_hash", "language", "parser_version"])
        await session.execute(stmt)
