"""Concurrent-review-generation regression test against a real
PostgreSQL database.

Guards against the exact bug this phase's own dogfooding caught: two
:class:`~patchfrog.persistence.models.review_memory.ReviewGenerationModel`
rows for the same PR racing to be created concurrently (e.g. two
overlapping incremental review runs, or a retried Celery delivery
racing the original) must never receive the same ``sequence_number`` --
:meth:`~patchfrog.persistence.repositories.review_generation.ReviewGenerationRepository.create`
serializes via a transaction-scoped ``pg_advisory_xact_lock`` keyed on
``pull_request_id``, mirroring the same pattern
:class:`~patchfrog.persistence.repositories.repository_index.RepositoryIndexRepository`
already uses for ``index_version``. SQLite (used elsewhere in this test
suite) has no real advisory locks and no real concurrency to race, so
this only means anything against a real Postgres engine -- skips
(rather than failing) if one isn't reachable at ``localhost:5432``.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import OperationalError, ProgrammingError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from patchfrog.persistence.models.pull_request import PullRequestModel
from patchfrog.persistence.models.repository import RepositoryModel
from patchfrog.persistence.models.repository_index import IndexStatus, RepositoryIndexModel
from patchfrog.persistence.models.review import ReviewRunModel
from patchfrog.persistence.models.review_memory import ReviewGenerationModel
from patchfrog.persistence.repositories.review_generation import ReviewGenerationRepository
from patchfrog.review.domain import ReviewRunStatus
from patchfrog.review_memory.config import NO_MEMORY_CONTEXT_FINGERPRINT
from patchfrog.review_memory.domain import IncrementalRunMode

_POSTGRES_URL = "postgresql+asyncpg://patchfrog:patchfrog@localhost:5432/patchfrog"


async def _postgres_available() -> AsyncEngine | None:
    engine = create_async_engine(_POSTGRES_URL)
    try:
        async with engine.begin() as conn:
            await conn.execute(text("SELECT 1 FROM review_generations LIMIT 1"))
    except (OperationalError, ProgrammingError):
        await engine.dispose()
        return None
    return engine


async def _make_review_run(
    session: AsyncSession, *, repository_id: uuid.UUID, repository_index_id: uuid.UUID, commit_sha: str
) -> uuid.UUID:
    model = ReviewRunModel(
        repository_id=repository_id, repository_index_id=repository_index_id, pull_request_id=None,
        commit_sha=commit_sha, config_fingerprint="cf", model_fingerprint="mf",
        incremental_context_fingerprint=NO_MEMORY_CONTEXT_FINGERPRINT, status=ReviewRunStatus.SUCCEEDED,
        reviewer_provider="fake", reviewer_model="fake-model", started_at=datetime.now(UTC),
    )
    session.add(model)
    await session.flush()
    return model.id


async def test_concurrent_generation_creation_never_collides_sequence_number() -> None:
    engine = await _postgres_available()
    if engine is None:
        pytest.skip("real PostgreSQL not reachable at localhost:5432 (docker compose up -d postgres)")

    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    full_name = f"review-memory-concurrency-test/{uuid.uuid4().hex[:8]}"
    repository_id: uuid.UUID | None = None

    try:
        async with session_factory() as session:
            repo = RepositoryModel(
                github_repository_id=uuid.uuid4().int & 0x7FFFFFFFFFFFFFFF, owner="test", name=full_name,
                full_name=full_name, installation_id=0,
            )
            session.add(repo)
            await session.flush()
            repository_id = repo.id

            index = RepositoryIndexModel(
                repository_id=repository_id, commit_sha="a" * 40, index_version=1,
                status=IndexStatus.SUCCEEDED, is_active=True, started_at=datetime.now(UTC),
            )
            session.add(index)
            await session.flush()

            pr = PullRequestModel(
                repository_id=repository_id, github_pr_number=1, title="t", author="a",
                base_sha="a" * 40, head_sha="a" * 40, state="open",
            )
            session.add(pr)
            await session.flush()
            pull_request_id = pr.id

            run_ids = [
                await _make_review_run(
                    session, repository_id=repository_id, repository_index_id=index.id,
                    commit_sha=f"{'a' * 39}{i}",
                )
                for i in range(5)
            ]
            await session.commit()

        repo_gen = ReviewGenerationRepository()

        async def _create(review_run_id: uuid.UUID, commit_sha: str) -> None:
            async with session_factory() as session:
                await repo_gen.create(
                    session, repository_id=repository_id, pull_request_id=pull_request_id,
                    review_run_id=review_run_id, commit_sha=commit_sha, previous_generation_id=None,
                    previous_commit_sha=None, ancestry_verified=False, mode=IncrementalRunMode.FULL,
                    compatibility_ok=False, invalidation_reason=None, memory_compatibility_fingerprint="fp",
                )
                await session.commit()

        await asyncio.gather(*(_create(run_ids[i], f"{'a' * 39}{i}") for i in range(5)))

        async with session_factory() as session:
            generations = (
                await session.execute(
                    select(ReviewGenerationModel).where(ReviewGenerationModel.pull_request_id == pull_request_id)
                )
            ).scalars().all()

        assert len(generations) == 5
        sequence_numbers = sorted(g.sequence_number for g in generations)
        assert sequence_numbers == [1, 2, 3, 4, 5]  # no gaps, no duplicates
    finally:
        if repository_id is not None:
            async with session_factory() as session:
                await session.execute(text("DELETE FROM review_generations WHERE repository_id = :id"), {"id": str(repository_id)})
                await session.execute(text("DELETE FROM review_runs WHERE repository_id = :id"), {"id": str(repository_id)})
                await session.execute(text("DELETE FROM pull_requests WHERE repository_id = :id"), {"id": str(repository_id)})
                await session.execute(text("DELETE FROM repository_indexes WHERE repository_id = :id"), {"id": str(repository_id)})
                await session.execute(text("DELETE FROM repositories WHERE id = :id"), {"id": str(repository_id)})
                await session.commit()
        await engine.dispose()
