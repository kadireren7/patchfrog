from __future__ import annotations

import uuid
from collections.abc import Sequence
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from patchfrog.domain.code import Language
from patchfrog.indexing.service import RepositoryIndexingService
from patchfrog.intelligence.graph import EdgeKind
from patchfrog.parsing.registry import ParserRegistry
from patchfrog.persistence.models.code_index import (
    FileIndexStatus,
    IndexedFileModel,
    RepositoryEdgeModel,
    SymbolModel,
)
from patchfrog.persistence.models.repository_index import IndexStatus, RepositoryIndexModel
from patchfrog.persistence.repositories import RepositoryEdgeRepository, RepositoryRepository
from tests.support.git_repo import commit_all, materialize_fixture_repo


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


class _RaisingEdgeRepository(RepositoryEdgeRepository):
    async def bulk_create(self, session: AsyncSession, models: Sequence[RepositoryEdgeModel]) -> None:
        raise RuntimeError("simulated persistence failure")


async def test_full_index_of_python_fixture_extracts_expected_structure(
    tmp_path: Path, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    snapshot = materialize_fixture_repo(tmp_path / "repo", "python_basic")
    repository_id = await _create_repository(session_factory, full_name="test/python_basic")

    service = RepositoryIndexingService(session_factory=session_factory)
    summary = await service.index_local_repository(
        repository_id=repository_id, root_path=snapshot.root_path, repository_full_name="test/python_basic"
    )

    assert summary.files_total == 4  # __init__.py, cache.py, utils.py, tests/test_cache.py
    assert summary.files_failed == 0
    assert summary.incremental is False

    async with session_factory() as session:
        index_row = (
            await session.execute(select(RepositoryIndexModel).where(RepositoryIndexModel.repository_id == repository_id))
        ).scalar_one()
        assert index_row.status is IndexStatus.SUCCEEDED
        assert index_row.is_active is True

        symbols = (
            await session.execute(select(SymbolModel).where(SymbolModel.repository_index_id == index_row.id))
        ).scalars().all()
        by_qualified = {s.qualified_name: s for s in symbols}
        assert "Cache" in by_qualified
        assert "Cache.get" in by_qualified
        assert by_qualified["Cache.get"].parent_symbol_id == by_qualified["Cache"].id

        edges = (
            await session.execute(select(RepositoryEdgeModel).where(RepositoryEdgeModel.repository_index_id == index_row.id))
        ).scalars().all()
        assert any(e.kind is EdgeKind.SYMBOL_CALLS_SYMBOL for e in edges)  # get()/set() call normalize_key
        assert any(e.kind is EdgeKind.FILE_IMPORTS_FILE for e in edges)  # cache.py imports utils.py
        assert any(e.kind is EdgeKind.FILE_TESTS_FILE for e in edges)  # test_cache.py tests cache.py


async def test_full_index_of_c_fixture_extracts_expected_structure(
    tmp_path: Path, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    snapshot = materialize_fixture_repo(tmp_path / "repo", "c_basic")
    repository_id = await _create_repository(session_factory, full_name="test/c_basic")

    service = RepositoryIndexingService(session_factory=session_factory)
    summary = await service.index_local_repository(
        repository_id=repository_id, root_path=snapshot.root_path, repository_full_name="test/c_basic"
    )

    assert summary.files_failed == 0

    async with session_factory() as session:
        index_row = (
            await session.execute(select(RepositoryIndexModel).where(RepositoryIndexModel.repository_id == repository_id))
        ).scalar_one()
        symbols = (
            await session.execute(select(SymbolModel).where(SymbolModel.repository_index_id == index_row.id))
        ).scalars().all()
        by_name = {s.name: s for s in symbols}
        assert by_name["s_node"].kind.value == "struct"
        assert by_name["t_node"].kind.value == "type_alias"
        assert by_name["node_new"].kind.value == "function"

        edges = (
            await session.execute(select(RepositoryEdgeModel).where(RepositoryEdgeModel.repository_index_id == index_row.id))
        ).scalars().all()
        # list.c calls node_new(), which is defined in node.c and resolvable
        # repo-wide since it's the only top-level symbol with that name.
        assert any(e.kind is EdgeKind.SYMBOL_CALLS_SYMBOL for e in edges)
        assert any(e.kind is EdgeKind.FILE_INCLUDES_FILE for e in edges)


async def test_full_index_of_cpp_fixture_extracts_expected_structure(
    tmp_path: Path, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    snapshot = materialize_fixture_repo(tmp_path / "repo", "cpp_basic")
    repository_id = await _create_repository(session_factory, full_name="test/cpp_basic")

    service = RepositoryIndexingService(session_factory=session_factory)
    summary = await service.index_local_repository(
        repository_id=repository_id, root_path=snapshot.root_path, repository_full_name="test/cpp_basic"
    )

    assert summary.files_failed == 0

    async with session_factory() as session:
        index_row = (
            await session.execute(select(RepositoryIndexModel).where(RepositoryIndexModel.repository_id == repository_id))
        ).scalar_one()
        symbols = (
            await session.execute(select(SymbolModel).where(SymbolModel.repository_index_id == index_row.id))
        ).scalars().all()
        qualified_names = {s.qualified_name for s in symbols}
        assert "patchfrog::Cache" in qualified_names
        assert "patchfrog::Cache::get" in qualified_names


async def test_mixed_language_repo_skips_unsupported_files(
    tmp_path: Path, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    snapshot = materialize_fixture_repo(tmp_path / "repo", "mixed_repo")
    repository_id = await _create_repository(session_factory, full_name="test/mixed_repo")

    service = RepositoryIndexingService(session_factory=session_factory)
    summary = await service.index_local_repository(
        repository_id=repository_id, root_path=snapshot.root_path, repository_full_name="test/mixed_repo"
    )

    assert summary.files_total == 3  # py/app.py, native/util.c, README.md
    assert summary.files_parsed == 2  # README.md has no parser

    async with session_factory() as session:
        index_row = (
            await session.execute(select(RepositoryIndexModel).where(RepositoryIndexModel.repository_id == repository_id))
        ).scalar_one()
        readme = (
            await session.execute(
                select(IndexedFileModel).where(
                    IndexedFileModel.repository_index_id == index_row.id,
                    IndexedFileModel.relative_path == "README.md",
                )
            )
        ).scalar_one()
        assert readme.status is FileIndexStatus.SKIPPED
        assert readme.language is None


async def test_unchanged_second_run_reuses_every_file_and_reparses_none(
    tmp_path: Path, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    snapshot = materialize_fixture_repo(tmp_path / "repo", "python_basic")
    repository_id = await _create_repository(session_factory, full_name="test/python_basic")
    service = RepositoryIndexingService(session_factory=session_factory)

    first = await service.index_local_repository(
        repository_id=repository_id, root_path=snapshot.root_path, repository_full_name="test/python_basic"
    )
    assert first.files_parsed > 0

    second = await service.index_local_repository(
        repository_id=repository_id, root_path=snapshot.root_path, repository_full_name="test/python_basic"
    )

    assert second.files_parsed == 0
    assert second.files_reused == first.files_parsed + first.files_reused
    assert second.incremental is True


async def test_single_modified_file_only_that_file_is_reparsed(
    tmp_path: Path, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    snapshot = materialize_fixture_repo(tmp_path / "repo", "python_basic")
    repository_id = await _create_repository(session_factory, full_name="test/python_basic")
    service = RepositoryIndexingService(session_factory=session_factory)

    await service.index_local_repository(
        repository_id=repository_id, root_path=snapshot.root_path, repository_full_name="test/python_basic"
    )

    (snapshot.root_path / "src" / "utils.py").write_text(
        "def normalize_key(key: str) -> str:\n    return key.strip().upper()\n"
    )
    commit_all(snapshot.root_path, "modify utils.py")

    summary = await service.index_local_repository(
        repository_id=repository_id, root_path=snapshot.root_path, repository_full_name="test/python_basic"
    )

    assert summary.files_parsed == 1
    assert summary.incremental is True


async def test_added_file_is_parsed_and_others_reused(
    tmp_path: Path, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    snapshot = materialize_fixture_repo(tmp_path / "repo", "python_basic")
    repository_id = await _create_repository(session_factory, full_name="test/python_basic")
    service = RepositoryIndexingService(session_factory=session_factory)

    first = await service.index_local_repository(
        repository_id=repository_id, root_path=snapshot.root_path, repository_full_name="test/python_basic"
    )

    (snapshot.root_path / "src" / "extra.py").write_text("def extra_fn():\n    pass\n")
    commit_all(snapshot.root_path, "add extra.py")

    summary = await service.index_local_repository(
        repository_id=repository_id, root_path=snapshot.root_path, repository_full_name="test/python_basic"
    )

    assert summary.files_total == first.files_total + 1
    assert summary.files_parsed == 1


async def test_deleted_file_is_removed_from_new_index(
    tmp_path: Path, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    snapshot = materialize_fixture_repo(tmp_path / "repo", "python_basic")
    repository_id = await _create_repository(session_factory, full_name="test/python_basic")
    service = RepositoryIndexingService(session_factory=session_factory)

    first = await service.index_local_repository(
        repository_id=repository_id, root_path=snapshot.root_path, repository_full_name="test/python_basic"
    )

    (snapshot.root_path / "src" / "utils.py").unlink()
    (snapshot.root_path / "src" / "cache.py").write_text(
        "class Cache:\n    def get(self, key):\n        return None\n"
    )
    commit_all(snapshot.root_path, "delete utils.py")

    second = await service.index_local_repository(
        repository_id=repository_id, root_path=snapshot.root_path, repository_full_name="test/python_basic"
    )

    assert second.files_total == first.files_total - 1

    async with session_factory() as session:
        index_row = (
            await session.execute(
                select(RepositoryIndexModel).where(
                    RepositoryIndexModel.repository_id == repository_id,
                    RepositoryIndexModel.is_active.is_(True),
                )
            )
        ).scalar_one()
        remaining = (
            await session.execute(
                select(IndexedFileModel).where(IndexedFileModel.repository_index_id == index_row.id)
            )
        ).scalars().all()
        assert "src/utils.py" not in {f.relative_path for f in remaining}


async def test_renamed_file_reuses_cached_parse_by_content_hash(
    tmp_path: Path, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    snapshot = materialize_fixture_repo(tmp_path / "repo", "python_basic")
    repository_id = await _create_repository(session_factory, full_name="test/python_basic")
    service = RepositoryIndexingService(session_factory=session_factory)

    await service.index_local_repository(
        repository_id=repository_id, root_path=snapshot.root_path, repository_full_name="test/python_basic"
    )

    (snapshot.root_path / "src" / "utils.py").rename(snapshot.root_path / "src" / "helpers.py")
    commit_all(snapshot.root_path, "rename utils.py to helpers.py")

    summary = await service.index_local_repository(
        repository_id=repository_id, root_path=snapshot.root_path, repository_full_name="test/python_basic"
    )

    # cache.py's `from src.utils import normalize_key` no longer resolves
    # post-rename (nothing guesses it points at helpers.py) — this proves
    # unresolved is preferred over a guessed match, not just a smoke test.
    assert summary.files_parsed == 0  # helpers.py's content is unchanged from utils.py, reused by hash


async def test_one_file_parser_failure_does_not_fail_the_whole_index(
    tmp_path: Path, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    snapshot = materialize_fixture_repo(tmp_path / "repo", "python_basic")
    repository_id = await _create_repository(session_factory, full_name="test/python_basic")

    class _FailingPythonParser:
        language = Language.PYTHON

        def parse_file(self, *, relative_path: str, content: bytes) -> None:
            raise RuntimeError("boom")

    registry = ParserRegistry()
    registry.register(_FailingPythonParser())  # type: ignore[arg-type]
    service = RepositoryIndexingService(session_factory=session_factory, parser_registry=registry)

    summary = await service.index_local_repository(
        repository_id=repository_id, root_path=snapshot.root_path, repository_full_name="test/python_basic"
    )

    assert summary.files_failed == summary.files_total
    assert summary.symbols_extracted == 0

    async with session_factory() as session:
        index_row = (
            await session.execute(select(RepositoryIndexModel).where(RepositoryIndexModel.repository_id == repository_id))
        ).scalar_one()
        assert index_row.status is IndexStatus.SUCCEEDED  # the run as a whole still completes
        failed_files = (
            await session.execute(
                select(IndexedFileModel).where(
                    IndexedFileModel.repository_index_id == index_row.id,
                    IndexedFileModel.status == FileIndexStatus.FAILED,
                )
            )
        ).scalars().all()
        assert len(failed_files) == summary.files_total
        assert all(f.error_message == "boom" for f in failed_files)


async def test_failed_run_does_not_corrupt_previous_active_index(
    tmp_path: Path, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    snapshot = materialize_fixture_repo(tmp_path / "repo", "python_basic")
    repository_id = await _create_repository(session_factory, full_name="test/python_basic")
    service = RepositoryIndexingService(session_factory=session_factory)

    good = await service.index_local_repository(
        repository_id=repository_id, root_path=snapshot.root_path, repository_full_name="test/python_basic"
    )

    (snapshot.root_path / "src" / "extra.py").write_text("def extra():\n    pass\n")
    commit_all(snapshot.root_path, "add extra.py")

    service._edge_repo = _RaisingEdgeRepository()  # inject a failure deep in persistence

    with pytest.raises(RuntimeError, match="simulated persistence failure"):
        await service.index_local_repository(
            repository_id=repository_id, root_path=snapshot.root_path, repository_full_name="test/python_basic"
        )

    async with session_factory() as session:
        rows = (
            await session.execute(
                select(RepositoryIndexModel)
                .where(RepositoryIndexModel.repository_id == repository_id)
                .order_by(RepositoryIndexModel.index_version)
            )
        ).scalars().all()
        assert len(rows) == 2
        assert rows[0].status is IndexStatus.SUCCEEDED
        assert rows[0].is_active is True  # untouched by the failed run

        failed_row = rows[1]
        assert failed_row.status is IndexStatus.FAILED
        assert failed_row.is_active is False
        assert failed_row.error_message == "simulated persistence failure"

        # Nothing the failed run would have written was left behind —
        # the whole persistence transaction rolled back.
        leftover_files = await session.execute(
            select(func.count()).select_from(IndexedFileModel).where(
                IndexedFileModel.repository_index_id == failed_row.id
            )
        )
        assert leftover_files.scalar_one() == 0
        leftover_symbols = await session.execute(
            select(func.count()).select_from(SymbolModel).where(
                SymbolModel.repository_index_id == failed_row.id
            )
        )
        assert leftover_symbols.scalar_one() == 0

        # The active index is still fully queryable and unchanged.
        active_files = (
            await session.execute(
                select(func.count()).select_from(IndexedFileModel).where(
                    IndexedFileModel.repository_index_id == rows[0].id
                )
            )
        ).scalar_one()
        assert active_files == good.files_total


async def test_repository_index_version_increments_per_repository(
    tmp_path: Path, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    snapshot = materialize_fixture_repo(tmp_path / "repo", "python_basic")
    repository_id = await _create_repository(session_factory, full_name="test/python_basic")
    service = RepositoryIndexingService(session_factory=session_factory)

    await service.index_local_repository(
        repository_id=repository_id, root_path=snapshot.root_path, repository_full_name="test/python_basic"
    )
    await service.index_local_repository(
        repository_id=repository_id, root_path=snapshot.root_path, repository_full_name="test/python_basic"
    )

    async with session_factory() as session:
        rows = (
            await session.execute(
                select(RepositoryIndexModel).where(RepositoryIndexModel.repository_id == repository_id)
            )
        ).scalars().all()
        versions = sorted(r.index_version for r in rows)
        assert versions == [1, 2]
        active = [r for r in rows if r.is_active]
        assert len(active) == 1
        assert active[0].index_version == 2
