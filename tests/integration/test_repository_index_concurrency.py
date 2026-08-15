"""Concurrent-indexing regression test against a real PostgreSQL database.

SQLite (used by every other integration test here) has no advisory locks
and no real MVCC — it cannot reproduce the race this test targets. This
test instead talks to the local `docker compose` PostgreSQL instance
directly and skips (rather than failing) if that isn't reachable, so it
stays opt-in for CI/dev environments without Docker running while still
being a real, non-mocked reproduction where it *is* available.

Bug this guards against: two concurrent indexing runs for the same
repository both compute ``next_version = MAX(index_version) + 1``
before either commits, so both attempt to insert the same
``(repository_id, index_version)`` — one gets a raw, unhandled
``IntegrityError`` from a code path that runs *before* the pipeline's
own try/except (see ``RepositoryIndexingService._start_run``). The fix
serializes that read-then-insert (and the later activate/deactivate
flip) with a transaction-scoped `pg_advisory_xact_lock` keyed by
``repository_id``, so a concurrent run waits its turn instead of racing.
"""

from __future__ import annotations

import asyncio
import shutil
import uuid
from pathlib import Path

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from patchfrog.indexing.service import RepositoryIndexingService
from patchfrog.persistence.models import Base
from patchfrog.persistence.models.repository_index import RepositoryIndexModel
from patchfrog.persistence.repositories import RepositoryRepository
from tests.support.git_repo import materialize_fixture_repo

_POSTGRES_URL = "postgresql+asyncpg://patchfrog:patchfrog@localhost:5432/patchfrog"


async def _postgres_available() -> AsyncEngine | None:
    engine = create_async_engine(_POSTGRES_URL)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all, checkfirst=True)
    except OperationalError:
        await engine.dispose()
        return None
    return engine


async def test_two_concurrent_indexing_runs_never_leave_two_active_indexes(
    tmp_path: Path,
) -> None:
    engine = await _postgres_available()
    if engine is None:
        pytest.skip("real PostgreSQL not reachable at localhost:5432 (docker compose up -d postgres)")

    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    full_name = f"concurrency-test/{uuid.uuid4().hex[:8]}"
    repository_id: uuid.UUID | None = None

    try:
        async with session_factory() as session:
            repo_row = await RepositoryRepository().upsert(
                session,
                github_repository_id=uuid.uuid4().int & 0x7FFFFFFFFFFFFFFF,
                owner="concurrency-test",
                name=full_name,
                full_name=full_name,
                installation_id=0,
            )
            await session.commit()
            repository_id = repo_row.id

        snapshot = materialize_fixture_repo(tmp_path / "repo", "python_basic", full_name=full_name)
        service = RepositoryIndexingService(session_factory=session_factory)

        results = await asyncio.gather(
            service.index_local_repository(
                repository_id=repository_id, root_path=snapshot.root_path, repository_full_name=full_name
            ),
            service.index_local_repository(
                repository_id=repository_id, root_path=snapshot.root_path, repository_full_name=full_name
            ),
            return_exceptions=True,
        )

        # Neither run should crash with a raw, unhandled DB exception —
        # both must complete (successfully or via a clean, caught failure).
        for result in results:
            if isinstance(result, BaseException):
                raise AssertionError(f"concurrent indexing run raised unexpectedly: {result!r}")

        async with session_factory() as session:
            rows = (
                await session.execute(
                    select(RepositoryIndexModel)
                    .where(RepositoryIndexModel.repository_id == repository_id)
                    .order_by(RepositoryIndexModel.index_version)
                )
            ).scalars().all()

            assert len(rows) == 2
            assert {r.index_version for r in rows} == {1, 2}  # no duplicate/skipped version
            active_rows = [r for r in rows if r.is_active]
            assert len(active_rows) == 1  # never two active indexes at once
    finally:
        if repository_id is not None:
            async with session_factory() as session:
                index_rows = (
                    await session.execute(
                        select(RepositoryIndexModel).where(RepositoryIndexModel.repository_id == repository_id)
                    )
                ).scalars().all()
                for row in index_rows:
                    await session.delete(row)  # cascades indexed_files/symbols/edges/etc.
                await session.flush()
                await session.execute(text("DELETE FROM repositories WHERE id = :id"), {"id": str(repository_id)})
                await session.commit()
        await engine.dispose()
        shutil.rmtree(tmp_path / "repo", ignore_errors=True)
