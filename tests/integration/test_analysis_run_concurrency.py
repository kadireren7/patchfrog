"""Concurrent-analysis regression test against a real PostgreSQL database.

Mirrors ``test_repository_index_concurrency.py`` — SQLite cannot
reproduce the race this test targets (no advisory locks, no real MVCC).
Skips (rather than failing) if a migrated Postgres isn't reachable at
``localhost:5432``; never creates schema itself (see that file's
docstring for why).

Guards against: two concurrent analysis requests for the exact same
``(repository_id, commit_sha, config_fingerprint)`` identity both racing
past the idempotency check and running analyzers/persisting findings
twice. ``AnalysisRunRepository.get_or_create_running`` and
``mark_succeeded`` serialize on that identity via a transaction-scoped
``pg_advisory_xact_lock``, so only one run ever becomes the canonical
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

from patchfrog.analysis.service import StaticAnalysisService
from patchfrog.indexing.service import RepositoryIndexingService
from patchfrog.persistence.models.analysis import AnalysisRunModel
from patchfrog.persistence.repositories import RepositoryRepository
from tests.support.git_repo import materialize_fixture_repo

_POSTGRES_URL = "postgresql+asyncpg://patchfrog:patchfrog@localhost:5432/patchfrog"


async def _postgres_available() -> AsyncEngine | None:
    engine = create_async_engine(_POSTGRES_URL)
    try:
        async with engine.begin() as conn:
            await conn.execute(text("SELECT 1 FROM findings LIMIT 1"))
    except (OperationalError, ProgrammingError):
        await engine.dispose()
        return None
    return engine


async def test_two_concurrent_analysis_runs_never_produce_two_succeeded_rows(
    tmp_path: Path,
) -> None:
    engine = await _postgres_available()
    if engine is None:
        pytest.skip("real PostgreSQL not reachable at localhost:5432 (docker compose up -d postgres)")

    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    full_name = f"analysis-concurrency-test/{uuid.uuid4().hex[:8]}"
    repository_id: uuid.UUID | None = None

    try:
        async with session_factory() as session:
            repo_row = await RepositoryRepository().upsert(
                session,
                github_repository_id=uuid.uuid4().int & 0x7FFFFFFFFFFFFFFF,
                owner="analysis-concurrency-test",
                name=full_name,
                full_name=full_name,
                installation_id=0,
            )
            await session.commit()
            repository_id = repo_row.id

        snapshot = materialize_fixture_repo(tmp_path / "repo", "static_python", full_name=full_name)
        await RepositoryIndexingService(session_factory=session_factory).index_local_repository(
            repository_id=repository_id, root_path=snapshot.root_path, repository_full_name=full_name
        )

        service = StaticAnalysisService(session_factory=session_factory)
        results = await asyncio.gather(
            service.analyze_local_repository(
                repository_id=repository_id, root_path=snapshot.root_path, repository_full_name=full_name
            ),
            service.analyze_local_repository(
                repository_id=repository_id, root_path=snapshot.root_path, repository_full_name=full_name
            ),
            return_exceptions=True,
        )

        for result in results:
            if isinstance(result, BaseException):
                raise AssertionError(f"concurrent analysis run raised unexpectedly: {result!r}")

        async with session_factory() as session:
            runs = (
                await session.execute(
                    select(AnalysisRunModel).where(AnalysisRunModel.repository_id == repository_id)
                )
            ).scalars().all()

            succeeded = [r for r in runs if r.status.value == "succeeded"]
            assert len(succeeded) == 1  # exactly one canonical succeeded run, never two

            # No duplicated finding data: the run that lost the race must
            # not have persisted its own separate findings set.
            from patchfrog.persistence.repositories import FindingRepository

            all_finding_counts = []
            for run in runs:
                findings = await FindingRepository().list_for_run(session, analysis_run_id=run.id)
                all_finding_counts.append(len(findings))
            # Only the canonical succeeded run should have findings attached.
            assert sum(all_finding_counts) == len(
                await FindingRepository().list_for_run(session, analysis_run_id=succeeded[0].id)
            )
    finally:
        if repository_id is not None:
            async with session_factory() as session:
                run_rows = (
                    await session.execute(
                        select(AnalysisRunModel).where(AnalysisRunModel.repository_id == repository_id)
                    )
                ).scalars().all()
                for row in run_rows:
                    await session.delete(row)
                await session.flush()
                await session.execute(text("DELETE FROM repository_indexes WHERE repository_id = :id"), {"id": str(repository_id)})
                await session.execute(text("DELETE FROM repositories WHERE id = :id"), {"id": str(repository_id)})
                await session.commit()
        await engine.dispose()
        shutil.rmtree(tmp_path / "repo", ignore_errors=True)
