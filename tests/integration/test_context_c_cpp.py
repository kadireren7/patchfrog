"""Basic multi-language sanity coverage for the Context Engine: C and C++
targets resolve and produce a bundle whose target item is correct. Kept
deliberately modest compared to the Python ranking suite
(``test_context_service.py``) -- C/C++ call resolution has real
declaration-vs-definition nuances (see
:mod:`patchfrog.intelligence.resolution`) that are Phase 2's concern, not
something to over-specify exact relationship kinds against here.
"""

from __future__ import annotations

import uuid
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from patchfrog.context.domain import ContextItemKind, ContextTargetType
from patchfrog.context.service import ContextService
from patchfrog.indexing.service import RepositoryIndexingService
from patchfrog.persistence.repositories import RepositoryRepository
from tests.support.git_repo import materialize_fixture_repo


async def _create_repository(session_factory: async_sessionmaker[AsyncSession], *, full_name: str) -> uuid.UUID:
    async with session_factory() as session:
        row = await RepositoryRepository().upsert(
            session,
            github_repository_id=abs(hash(full_name)) % (2**62),
            owner=full_name.split("/")[0],
            name=full_name.split("/")[-1],
            full_name=full_name,
            installation_id=0,
        )
        await session.commit()
        return row.id


async def _index(session_factory: async_sessionmaker[AsyncSession], *, repository_id: uuid.UUID, root_path: Path, full_name: str) -> None:
    await RepositoryIndexingService(session_factory=session_factory).index_local_repository(
        repository_id=repository_id, root_path=root_path, repository_full_name=full_name
    )


async def test_c_target_resolves_to_the_containing_function(
    tmp_path: Path, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    snapshot = materialize_fixture_repo(tmp_path / "repo", "context_c")
    repository_id = await _create_repository(session_factory, full_name="test/context-c")
    await _index(session_factory, repository_id=repository_id, root_path=snapshot.root_path, full_name="test/context-c")

    service = ContextService(session_factory=session_factory)
    bundle = await service.build_context_local(
        repository_id=repository_id,
        root_path=snapshot.root_path,
        repository_full_name="test/context-c",
        target_type=ContextTargetType.LINE,
        file_path="src/list.c",
        line=8,  # inside list_insert
    )

    assert bundle.items[0].kind is ContextItemKind.TARGET_SYMBOL
    assert bundle.items[0].qualified_name is not None and "list_insert" in bundle.items[0].qualified_name


async def test_cpp_target_resolves_to_the_containing_method(
    tmp_path: Path, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    snapshot = materialize_fixture_repo(tmp_path / "repo", "context_cpp")
    repository_id = await _create_repository(session_factory, full_name="test/context-cpp")
    await _index(session_factory, repository_id=repository_id, root_path=snapshot.root_path, full_name="test/context-cpp")

    service = ContextService(session_factory=session_factory)
    bundle = await service.build_context_local(
        repository_id=repository_id,
        root_path=snapshot.root_path,
        repository_full_name="test/context-cpp",
        target_type=ContextTargetType.LINE,
        file_path="src/cache.cpp",
        line=8,  # inside Cache::insert
    )

    assert bundle.items[0].kind is ContextItemKind.TARGET_SYMBOL
    assert bundle.items[0].qualified_name is not None and "insert" in bundle.items[0].qualified_name
