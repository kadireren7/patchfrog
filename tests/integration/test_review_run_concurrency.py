"""Concurrent-AI-review regression test against a real PostgreSQL
database.

Mirrors ``tests/integration/test_context_concurrency.py`` and
``tests/integration/test_analysis_run_concurrency.py`` exactly -- SQLite
cannot reproduce the race this test targets (no advisory locks, no real
MVCC). Skips (rather than failing) if a migrated Postgres isn't reachable
at ``localhost:5432``; never creates schema itself.

Guards against: two concurrent review requests for the exact same
``(repository_id, commit_sha, config_fingerprint, model_fingerprint)``
identity both racing past the idempotency check and persisting findings
twice -- e.g. a duplicate Celery task delivery.
``ReviewRunRepository.get_or_create_running``/``mark_succeeded`` serialize
on that identity via a transaction-scoped ``pg_advisory_xact_lock``, so
only one review run ever becomes the canonical ``succeeded`` row.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import uuid
from pathlib import Path

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import OperationalError, ProgrammingError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from patchfrog.diff.models import DiffFile, DiffHunk, DiffLine, DiffLineType
from patchfrog.indexing.service import RepositoryIndexingService
from patchfrog.persistence.models.review import ReviewCandidateModel, ReviewRunModel
from patchfrog.persistence.repositories import RepositoryRepository
from patchfrog.review.domain import ReviewRunStatus
from patchfrog.review.providers.fake import FakeLLMProvider, ScriptedResponse
from patchfrog.review.service import PullRequestReviewService
from tests.support.git_repo import materialize_fixture_repo

_POSTGRES_URL = "postgresql+asyncpg://patchfrog:patchfrog@localhost:5432/patchfrog"


def _diff_marking_lines(file_path: str, lines: list[int]) -> DiffFile:
    diff_lines = tuple(
        DiffLine(line_type=DiffLineType.ADDITION, old_line_number=None, new_line_number=n, content="x")
        for n in lines
    )
    hunk = DiffHunk(
        old_start=1, old_lines=0, new_start=min(lines), new_lines=len(lines),
        section_heading=None, lines=diff_lines,
    )
    return DiffFile(path=file_path, hunks=(hunk,))


async def _postgres_available() -> AsyncEngine | None:
    engine = create_async_engine(_POSTGRES_URL)
    try:
        async with engine.begin() as conn:
            await conn.execute(text("SELECT 1 FROM review_runs LIMIT 1"))
    except (OperationalError, ProgrammingError):
        await engine.dispose()
        return None
    return engine


async def test_two_concurrent_reviews_never_produce_two_succeeded_runs(tmp_path: Path) -> None:
    engine = await _postgres_available()
    if engine is None:
        pytest.skip("real PostgreSQL not reachable at localhost:5432 (docker compose up -d postgres)")

    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    full_name = f"review-concurrency-test/{uuid.uuid4().hex[:8]}"
    repository_id: uuid.UUID | None = None

    try:
        async with session_factory() as session:
            repo_row = await RepositoryRepository().upsert(
                session,
                github_repository_id=uuid.uuid4().int & 0x7FFFFFFFFFFFFFFF,
                owner="review-concurrency-test",
                name=full_name,
                full_name=full_name,
                installation_id=0,
            )
            await session.commit()
            repository_id = repo_row.id

        snapshot = materialize_fixture_repo(tmp_path / "repo", "ai_review_python", full_name=full_name)
        await RepositoryIndexingService(session_factory=session_factory).index_local_repository(
            repository_id=repository_id, root_path=snapshot.root_path, repository_full_name=full_name
        )

        diff_files = [_diff_marking_lines("src/billing.py", [14])]
        provider = FakeLLMProvider(
            response_factory=lambda req: ScriptedResponse(raw_json=json.dumps({"findings": []}))
        )
        service = PullRequestReviewService(session_factory=session_factory, reviewer_provider=provider)

        results = await asyncio.gather(
            service.review_local(
                repository_id=repository_id, root_path=snapshot.root_path, repository_full_name=full_name,
                commit_sha=snapshot.commit_sha, diff_files=diff_files,
            ),
            service.review_local(
                repository_id=repository_id, root_path=snapshot.root_path, repository_full_name=full_name,
                commit_sha=snapshot.commit_sha, diff_files=diff_files,
            ),
            return_exceptions=True,
        )

        for result in results:
            if isinstance(result, BaseException):
                raise AssertionError(f"concurrent review raised unexpectedly: {result!r}")

        async with session_factory() as session:
            runs = (
                (await session.execute(select(ReviewRunModel).where(ReviewRunModel.repository_id == repository_id)))
                .scalars()
                .all()
            )
            succeeded = [r for r in runs if r.status == ReviewRunStatus.SUCCEEDED]
            assert len(succeeded) == 1  # exactly one canonical succeeded run, never two

            for run in runs:
                candidates = (
                    await session.execute(
                        select(ReviewCandidateModel).where(ReviewCandidateModel.review_run_id == run.id)
                    )
                ).scalars().all()
                if run.status == ReviewRunStatus.SUCCEEDED:
                    assert len(candidates) == 1  # canonical run persisted its candidate row
                else:
                    assert len(candidates) == 0  # losing run never persisted any rows
    finally:
        if repository_id is not None:
            async with session_factory() as session:
                run_rows = (
                    await session.execute(select(ReviewRunModel).where(ReviewRunModel.repository_id == repository_id))
                ).scalars().all()
                for row in run_rows:
                    await session.delete(row)
                await session.flush()
                # Context bundles were created as a side effect of review
                # (context-building per candidate) and reference
                # repository_indexes -- must go before that delete.
                await session.execute(
                    text("DELETE FROM context_bundles WHERE repository_id = :id"), {"id": str(repository_id)}
                )
                await session.execute(text("DELETE FROM repository_indexes WHERE repository_id = :id"), {"id": str(repository_id)})
                await session.execute(text("DELETE FROM repositories WHERE id = :id"), {"id": str(repository_id)})
                await session.commit()
        await engine.dispose()
        shutil.rmtree(tmp_path / "repo", ignore_errors=True)
