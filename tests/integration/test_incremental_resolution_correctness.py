"""Adversarial tests: does resolution/graph state stay correct across an
incremental re-index when only *one side* of a relationship changes?

The parse cache (see :mod:`patchfrog.indexing.parse_cache`) means an
unchanged file's *parsed structure* is reused verbatim across runs — but
resolution and graph construction run fresh every time, over every
currently parsed file, specifically so a change on one side of a
call/import relationship is reflected even though the other side's file
was never reparsed. These tests prove that design assumption rather than
just asserting it.
"""

from __future__ import annotations

import uuid
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from patchfrog.indexing.service import RepositoryIndexingService
from patchfrog.intelligence.graph import EdgeKind
from patchfrog.intelligence.queries import RepositoryQueryService
from patchfrog.persistence.repositories import RepositoryEdgeRepository, RepositoryRepository
from tests.support.git_repo import commit_all, init_git_repo


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


def _write(root: Path, relative_path: str, content: str) -> None:
    path = root / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


async def test_callee_renamed_in_untouched_caller_becomes_unresolved(
    tmp_path: Path, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    _write(root, "a.py", "from b import foo\n\n\ndef run():\n    return foo()\n")
    _write(root, "b.py", "def foo():\n    return 1\n")
    init_git_repo(root)
    commit_all(root, "initial")

    repository_id = await _create_repository(session_factory, full_name="test/callee-rename")
    service = RepositoryIndexingService(session_factory=session_factory)
    await service.index_local_repository(repository_id=repository_id, root_path=root, repository_full_name="x")

    queries = RepositoryQueryService()
    async with session_factory() as session:
        index_before = await queries.get_active_index(session, repository_id=repository_id)
        assert index_before is not None
        run_symbol = (
            await queries.find_symbol_by_qualified_name(session, repository_index_id=index_before.id, qualified_name="run")
        )[0]
        callees_before = await queries.get_callees(session, symbol_id=run_symbol.id)
        assert {c.callee_name for c in callees_before} == {"foo"}
        assert callees_before[0].resolution_status.value == "resolved"

    # a.py is untouched (its parse is reused from cache); only b.py changes.
    _write(root, "b.py", "def bar():\n    return 1\n")
    commit_all(root, "rename foo to bar in b.py")

    await service.index_local_repository(repository_id=repository_id, root_path=root, repository_full_name="x")

    async with session_factory() as session:
        index_after = await queries.get_active_index(session, repository_id=repository_id)
        assert index_after is not None
        run_symbol_after = (
            await queries.find_symbol_by_qualified_name(session, repository_index_id=index_after.id, qualified_name="run")
        )[0]
        callees_after = await queries.get_callees(session, symbol_id=run_symbol_after.id)

        assert len(callees_after) == 1
        assert callees_after[0].callee_name == "foo"
        assert callees_after[0].resolution_status.value == "unresolved"
        assert callees_after[0].resolved_symbol_id is None

        edges = await RepositoryEdgeRepository().edges_of_kind(
            session, repository_index_id=index_after.id, kind=EdgeKind.SYMBOL_CALLS_SYMBOL
        )
        assert edges == []  # the stale call edge must not survive


async def test_symbol_removed_entirely_makes_its_callers_unresolved(
    tmp_path: Path, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    _write(root, "a.py", "def run():\n    return helper()\n\n\ndef helper():\n    return 1\n")
    init_git_repo(root)
    commit_all(root, "initial")

    repository_id = await _create_repository(session_factory, full_name="test/symbol-removed")
    service = RepositoryIndexingService(session_factory=session_factory)
    await service.index_local_repository(repository_id=repository_id, root_path=root, repository_full_name="x")

    queries = RepositoryQueryService()
    async with session_factory() as session:
        index_before = await queries.get_active_index(session, repository_id=repository_id)
        assert index_before is not None
        by_name = await queries.find_symbol_by_name(session, repository_index_id=index_before.id, name="helper")
        assert len(by_name) == 1
        callers_before = await queries.get_callers(session, symbol_id=by_name[0].id)
        assert len(callers_before) == 1

    _write(root, "a.py", "def run():\n    return helper()\n")  # helper() no longer defined anywhere
    commit_all(root, "remove helper")

    await service.index_local_repository(repository_id=repository_id, root_path=root, repository_full_name="x")

    async with session_factory() as session:
        index_after = await queries.get_active_index(session, repository_id=repository_id)
        assert index_after is not None
        helper_symbols = await queries.find_symbol_by_name(session, repository_index_id=index_after.id, name="helper")
        assert helper_symbols == []  # the removed symbol must not still exist in the new index

        run_symbol = (
            await queries.find_symbol_by_qualified_name(session, repository_index_id=index_after.id, qualified_name="run")
        )[0]
        callees_after = await queries.get_callees(session, symbol_id=run_symbol.id)
        assert len(callees_after) == 1
        assert callees_after[0].resolution_status.value == "unresolved"


async def test_ambiguity_introduced_stops_previously_resolved_call_from_resolving(
    tmp_path: Path, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    _write(root, "a.py", "def run():\n    return foo()\n")
    _write(root, "b.py", "def foo():\n    return 1\n")
    init_git_repo(root)
    commit_all(root, "initial")

    repository_id = await _create_repository(session_factory, full_name="test/ambiguity-introduced")
    service = RepositoryIndexingService(session_factory=session_factory)
    await service.index_local_repository(repository_id=repository_id, root_path=root, repository_full_name="x")

    queries = RepositoryQueryService()
    async with session_factory() as session:
        index_before = await queries.get_active_index(session, repository_id=repository_id)
        assert index_before is not None
        run_symbol = (
            await queries.find_symbol_by_qualified_name(session, repository_index_id=index_before.id, qualified_name="run")
        )[0]
        callees_before = await queries.get_callees(session, symbol_id=run_symbol.id)
        assert callees_before[0].resolution_status.value == "resolved"

    # a.py (the caller) is untouched; a *new* file introduces a second
    # plausible `foo()` — the previously unique repo-wide match is gone.
    _write(root, "c.py", "def foo():\n    return 2\n")
    commit_all(root, "introduce a second foo()")

    await service.index_local_repository(repository_id=repository_id, root_path=root, repository_full_name="x")

    async with session_factory() as session:
        index_after = await queries.get_active_index(session, repository_id=repository_id)
        assert index_after is not None
        run_symbol_after = (
            await queries.find_symbol_by_qualified_name(session, repository_index_id=index_after.id, qualified_name="run")
        )[0]
        callees_after = await queries.get_callees(session, symbol_id=run_symbol_after.id)

        assert len(callees_after) == 1
        # Must not arbitrarily pick one of the two now-ambiguous candidates.
        assert callees_after[0].resolution_status.value == "ambiguous"
        assert callees_after[0].resolved_symbol_id is None


async def test_ambiguity_removed_lets_a_previously_ambiguous_call_resolve(
    tmp_path: Path, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    _write(root, "a.py", "def run():\n    return foo()\n")
    _write(root, "b.py", "def foo():\n    return 1\n")
    _write(root, "c.py", "def foo():\n    return 2\n")
    init_git_repo(root)
    commit_all(root, "initial")

    repository_id = await _create_repository(session_factory, full_name="test/ambiguity-removed")
    service = RepositoryIndexingService(session_factory=session_factory)
    await service.index_local_repository(repository_id=repository_id, root_path=root, repository_full_name="x")

    queries = RepositoryQueryService()
    async with session_factory() as session:
        index_before = await queries.get_active_index(session, repository_id=repository_id)
        assert index_before is not None
        run_symbol = (
            await queries.find_symbol_by_qualified_name(session, repository_index_id=index_before.id, qualified_name="run")
        )[0]
        callees_before = await queries.get_callees(session, symbol_id=run_symbol.id)
        assert callees_before[0].resolution_status.value == "ambiguous"

    (root / "c.py").unlink()
    commit_all(root, "remove the ambiguous second foo()")

    await service.index_local_repository(repository_id=repository_id, root_path=root, repository_full_name="x")

    async with session_factory() as session:
        index_after = await queries.get_active_index(session, repository_id=repository_id)
        assert index_after is not None
        run_symbol_after = (
            await queries.find_symbol_by_qualified_name(session, repository_index_id=index_after.id, qualified_name="run")
        )[0]
        callees_after = await queries.get_callees(session, symbol_id=run_symbol_after.id)

        assert len(callees_after) == 1
        assert callees_after[0].resolution_status.value == "resolved"


async def test_import_target_moved_drops_stale_file_edge(
    tmp_path: Path, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    _write(root, "a.py", "from b import foo\n")
    _write(root, "b.py", "def foo():\n    return 1\n")
    init_git_repo(root)
    commit_all(root, "initial")

    repository_id = await _create_repository(session_factory, full_name="test/import-moved")
    service = RepositoryIndexingService(session_factory=session_factory)
    await service.index_local_repository(repository_id=repository_id, root_path=root, repository_full_name="x")

    queries = RepositoryQueryService()
    async with session_factory() as session:
        index_before = await queries.get_active_index(session, repository_id=repository_id)
        assert index_before is not None
        a_file = await queries.get_file(session, repository_index_id=index_before.id, relative_path="a.py")
        b_file = await queries.get_file(session, repository_index_id=index_before.id, relative_path="b.py")
        assert a_file is not None and b_file is not None
        importers_before = await queries.files_importing(session, indexed_file_id=b_file.id)
        assert any(i.indexed_file_id == a_file.id for i in importers_before)

    # a.py (the importer) is untouched; b.py is deleted (moved elsewhere
    # without updating a.py, since nothing rewrites import statements).
    (root / "b.py").unlink()
    commit_all(root, "delete b.py")

    await service.index_local_repository(repository_id=repository_id, root_path=root, repository_full_name="x")

    async with session_factory() as session:
        index_after = await queries.get_active_index(session, repository_id=repository_id)
        assert index_after is not None
        b_file_after = await queries.get_file(session, repository_index_id=index_after.id, relative_path="b.py")
        assert b_file_after is None  # deleted file must not still exist in the new index

        a_file_after = await queries.get_file(session, repository_index_id=index_after.id, relative_path="a.py")
        assert a_file_after is not None
        imports_after = await queries.imports_from_file(session, indexed_file_id=a_file_after.id)
        assert len(imports_after) == 1
        assert imports_after[0].resolved_file_id is None  # the import is now correctly unresolved
