"""Regression tests for the parse cache's staleness-safety key.

Bug this guards against: before ``parser_version`` was part of the cache
key, a parser bugfix (like the header-guard extraction fix found during
Phase 2's own audit) would never take effect for any file whose content
hash was already cached — the stale, pre-fix parse would be served
forever. See :data:`patchfrog.parsing.base.PARSER_VERSION`.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from patchfrog.domain.code import Language
from patchfrog.persistence.repositories import ParsedFileCacheRepository


async def test_same_content_and_language_but_different_parser_version_is_a_cache_miss(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    repo = ParsedFileCacheRepository()

    async with session_factory() as session:
        await repo.put(
            session, content_hash="abc123", language=Language.PYTHON, parser_version=1, payload="v1-payload"
        )
        await session.commit()

    async with session_factory() as session:
        hit_same_version = await repo.get(
            session, content_hash="abc123", language=Language.PYTHON, parser_version=1
        )
        miss_new_version = await repo.get(
            session, content_hash="abc123", language=Language.PYTHON, parser_version=2
        )

    assert hit_same_version == "v1-payload"
    assert miss_new_version is None


async def test_same_content_hash_different_language_does_not_cross_contaminate(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    repo = ParsedFileCacheRepository()

    async with session_factory() as session:
        await repo.put(session, content_hash="same-hash", language=Language.PYTHON, parser_version=1, payload="python-payload")
        await repo.put(session, content_hash="same-hash", language=Language.C, parser_version=1, payload="c-payload")
        await session.commit()

    async with session_factory() as session:
        python_hit = await repo.get(session, content_hash="same-hash", language=Language.PYTHON, parser_version=1)
        c_hit = await repo.get(session, content_hash="same-hash", language=Language.C, parser_version=1)

    assert python_hit == "python-payload"
    assert c_hit == "c-payload"
