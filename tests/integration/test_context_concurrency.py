"""Concurrent-context-generation regression test against a real
PostgreSQL database.

Mirrors ``tests/integration/test_analysis_run_concurrency.py`` -- SQLite
cannot reproduce the race this test targets (no advisory locks, no real
MVCC). Skips (rather than failing) if a migrated Postgres isn't reachable
at ``localhost:5432``; never creates schema itself.

Guards against: two concurrent context-generation requests for the exact
same ``(repository_id, commit_sha, target_fingerprint, config_fingerprint)``
identity both racing past the idempotency check and persisting items
twice. ``ContextBundleRepository.get_or_create_running``/``mark_succeeded``
serialize on that identity via a transaction-scoped
``pg_advisory_xact_lock``, so only one bundle ever becomes the canonical
``succeeded`` row for that identity.
"""

from __future__ import annotations

import asyncio
import shutil
import uuid
from pathlib import Path

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import OperationalError, ProgrammingError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from patchfrog.context.domain import ContextTargetType
from patchfrog.context.service import ContextService
from patchfrog.indexing.service import RepositoryIndexingService
from patchfrog.persistence.models.context import ContextBundleModel, ContextItemModel
from patchfrog.persistence.repositories import RepositoryRepository
from tests.support.git_repo import materialize_fixture_repo

_POSTGRES_URL = "postgresql+asyncpg://patchfrog:patchfrog@localhost:5432/patchfrog"


async def _postgres_available() -> AsyncEngine | None:
    engine = create_async_engine(_POSTGRES_URL)
    try:
        async with engine.begin() as conn:
            await conn.execute(text("SELECT 1 FROM context_bundles LIMIT 1"))
    except (OperationalError, ProgrammingError):
        await engine.dispose()
        return None
    return engine


async def test_two_concurrent_context_generations_never_produce_two_succeeded_bundles(
    tmp_path: Path,
) -> None:
    engine = await _postgres_available()
    if engine is None:
        pytest.skip("real PostgreSQL not reachable at localhost:5432 (docker compose up -d postgres)")

    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    full_name = f"context-concurrency-test/{uuid.uuid4().hex[:8]}"
    repository_id: uuid.UUID | None = None

    try:
        async with session_factory() as session:
            repo_row = await RepositoryRepository().upsert(
                session,
                github_repository_id=uuid.uuid4().int & 0x7FFFFFFFFFFFFFFF,
                owner="context-concurrency-test",
                name=full_name,
                full_name=full_name,
                installation_id=0,
            )
            await session.commit()
            repository_id = repo_row.id

        snapshot = materialize_fixture_repo(tmp_path / "repo", "context_python", full_name=full_name)
        await RepositoryIndexingService(session_factory=session_factory).index_local_repository(
            repository_id=repository_id, root_path=snapshot.root_path, repository_full_name=full_name
        )

        service = ContextService(session_factory=session_factory)
        results = await asyncio.gather(
            service.build_context_local(
                repository_id=repository_id, root_path=snapshot.root_path, repository_full_name=full_name,
                target_type=ContextTargetType.LINE, file_path="src/cache.py", line=8,
            ),
            service.build_context_local(
                repository_id=repository_id, root_path=snapshot.root_path, repository_full_name=full_name,
                target_type=ContextTargetType.LINE, file_path="src/cache.py", line=8,
            ),
            return_exceptions=True,
        )

        for result in results:
            if isinstance(result, BaseException):
                raise AssertionError(f"concurrent context generation raised unexpectedly: {result!r}")

        async with session_factory() as session:
            bundles = (
                await session.execute(
                    select(ContextBundleModel).where(ContextBundleModel.repository_id == repository_id)
                )
            ).scalars().all()

            succeeded = [b for b in bundles if b.status.value == "succeeded"]
            assert len(succeeded) == 1  # exactly one canonical succeeded bundle, never two

            all_item_counts = []
            for bundle in bundles:
                items = (
                    await session.execute(
                        select(ContextItemModel).where(ContextItemModel.bundle_id == bundle.id)
                    )
                ).scalars().all()
                all_item_counts.append(len(items))
            succeeded_items = (
                await session.execute(
                    select(ContextItemModel).where(ContextItemModel.bundle_id == succeeded[0].id)
                )
            ).scalars().all()
            # Only the canonical succeeded bundle should have items attached.
            assert sum(all_item_counts) == len(succeeded_items)
    finally:
        if repository_id is not None:
            async with session_factory() as session:
                bundle_rows = (
                    await session.execute(
                        select(ContextBundleModel).where(ContextBundleModel.repository_id == repository_id)
                    )
                ).scalars().all()
                for row in bundle_rows:
                    await session.delete(row)
                await session.flush()
                await session.execute(text("DELETE FROM repository_indexes WHERE repository_id = :id"), {"id": str(repository_id)})
                await session.execute(text("DELETE FROM repositories WHERE id = :id"), {"id": str(repository_id)})
                await session.commit()
        await engine.dispose()
        shutil.rmtree(tmp_path / "repo", ignore_errors=True)
