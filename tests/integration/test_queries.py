from __future__ import annotations

import uuid
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from patchfrog.indexing.service import RepositoryIndexingService
from patchfrog.intelligence.queries import RepositoryQueryService
from patchfrog.persistence.repositories import RepositoryRepository
from tests.support.git_repo import materialize_fixture_repo


async def _create_repository(
    session_factory: async_sessionmaker[AsyncSession], *, full_name: str
) -> uuid.UUID:
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


async def _index_python_basic(
    tmp_path: Path, session_factory: async_sessionmaker[AsyncSession]
) -> uuid.UUID:
    """Index the python_basic fixture and return the resulting index id."""

    snapshot = materialize_fixture_repo(tmp_path / "repo", "python_basic")
    repository_id = await _create_repository(session_factory, full_name="test/python_basic")
    service = RepositoryIndexingService(session_factory=session_factory)
    await service.index_local_repository(
        repository_id=repository_id, root_path=snapshot.root_path, repository_full_name="test/python_basic"
    )

    queries = RepositoryQueryService()
    async with session_factory() as session:
        index_row = await queries.get_active_index(session, repository_id=repository_id)
        assert index_row is not None
        return index_row.id


async def test_symbols_in_file_returns_all_symbols_for_that_file(
    tmp_path: Path, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    index_id = await _index_python_basic(tmp_path, session_factory)
    queries = RepositoryQueryService()

    async with session_factory() as session:
        cache_file = await queries.get_file(session, repository_index_id=index_id, relative_path="src/cache.py")
        assert cache_file is not None

        symbols = await queries.symbols_in_file(session, indexed_file_id=cache_file.id)

    qualified_names = {s.qualified_name for s in symbols}
    assert qualified_names == {"Cache", "Cache.__init__", "Cache.get", "Cache.set"}


async def test_find_symbol_by_name_and_qualified_name(
    tmp_path: Path, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    index_id = await _index_python_basic(tmp_path, session_factory)
    queries = RepositoryQueryService()

    async with session_factory() as session:
        by_name = await queries.find_symbol_by_name(session, repository_index_id=index_id, name="get")
        assert {s.qualified_name for s in by_name} == {"Cache.get"}

        by_qualified = await queries.find_symbol_by_qualified_name(
            session, repository_index_id=index_id, qualified_name="Cache.get"
        )
        assert len(by_qualified) == 1
        assert by_qualified[0].name == "get"


async def test_symbol_containing_line_maps_a_line_to_its_function(
    tmp_path: Path, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    index_id = await _index_python_basic(tmp_path, session_factory)
    queries = RepositoryQueryService()

    async with session_factory() as session:
        symbol = await queries.symbol_for_changed_line(
            session, repository_index_id=index_id, relative_path="src/cache.py", line=8
        )

    assert symbol is not None
    assert symbol.qualified_name == "Cache.get"


async def test_symbol_containing_line_returns_none_outside_any_symbol(
    tmp_path: Path, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    index_id = await _index_python_basic(tmp_path, session_factory)
    queries = RepositoryQueryService()

    async with session_factory() as session:
        symbol = await queries.symbol_for_changed_line(
            session, repository_index_id=index_id, relative_path="src/cache.py", line=1
        )

    assert symbol is None  # line 1 is the top-level `from src.utils import normalize_key`


async def test_symbol_containing_line_returns_none_for_unindexed_file(
    tmp_path: Path, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    index_id = await _index_python_basic(tmp_path, session_factory)
    queries = RepositoryQueryService()

    async with session_factory() as session:
        symbol = await queries.symbol_for_changed_line(
            session, repository_index_id=index_id, relative_path="does/not/exist.py", line=1
        )

    assert symbol is None


async def test_get_callers_and_callees(
    tmp_path: Path, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    index_id = await _index_python_basic(tmp_path, session_factory)
    queries = RepositoryQueryService()

    async with session_factory() as session:
        normalize_key = (
            await queries.find_symbol_by_qualified_name(
                session, repository_index_id=index_id, qualified_name="normalize_key"
            )
        )[0]
        callers = await queries.get_callers(session, symbol_id=normalize_key.id)
        assert {c.callee_name for c in callers} == {"normalize_key"}
        assert len(callers) == 2  # Cache.get and Cache.set both call it

        cache_get = (
            await queries.find_symbol_by_qualified_name(
                session, repository_index_id=index_id, qualified_name="Cache.get"
            )
        )[0]
        callees = await queries.get_callees(session, symbol_id=cache_get.id)
        assert {c.callee_name for c in callees} == {"normalize_key", "get"}  # self._store.get(...)


async def test_imports_from_file_and_files_importing(
    tmp_path: Path, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    index_id = await _index_python_basic(tmp_path, session_factory)
    queries = RepositoryQueryService()

    async with session_factory() as session:
        cache_file = await queries.get_file(session, repository_index_id=index_id, relative_path="src/cache.py")
        utils_file = await queries.get_file(session, repository_index_id=index_id, relative_path="src/utils.py")
        assert cache_file is not None and utils_file is not None

        imports = await queries.imports_from_file(session, indexed_file_id=cache_file.id)
        assert any(i.resolved_file_id == utils_file.id for i in imports)

        importers = await queries.files_importing(session, indexed_file_id=utils_file.id)
        assert any(i.indexed_file_id == cache_file.id for i in importers)


async def test_likely_tests_for_file(
    tmp_path: Path, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    index_id = await _index_python_basic(tmp_path, session_factory)
    queries = RepositoryQueryService()

    async with session_factory() as session:
        cache_file = await queries.get_file(session, repository_index_id=index_id, relative_path="src/cache.py")
        assert cache_file is not None

        test_edges = await queries.likely_tests_for_file(session, indexed_file_id=cache_file.id)
        test_file = await queries.get_file(
            session, repository_index_id=index_id, relative_path="tests/test_cache.py"
        )

    assert test_file is not None
    assert any(e.source_file_id == test_file.id for e in test_edges)
