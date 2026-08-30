"""Real-Postgres coverage for ``review_candidates.context_bundle_id``'s
FK/cascade lifecycle.

Mirrors ``test_review_run_concurrency.py`` -- SQLite's test engine does
not enforce foreign keys by default, so this cascade behavior can only be
verified for real against Postgres. Skips (rather than failing) if a
migrated Postgres isn't reachable at ``localhost:5432``.

Guards against: deleting a ``review_runs`` row (which cascades to its
``review_candidates`` rows via ``ondelete="CASCADE"``) must never cascade
further into the referenced ``context_bundles`` row -- that table is
owned by the Context Engine (Phase 4), has its own independent lifecycle,
and is very likely referenced by more than one review candidate/run over
time (the whole point of ``reused_existing_bundle``). The FK from
``review_candidates.context_bundle_id`` to ``context_bundles.id`` has no
``ondelete`` clause (default ``NO ACTION``/``RESTRICT``), so a bundle
still referenced by a *surviving* row could not even be deleted directly
-- but the concern this test targets is the other direction: cascading
*from* review_runs must stop at review_candidates.
"""

from __future__ import annotations

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
from patchfrog.persistence.models.context import ContextBundleModel
from patchfrog.persistence.models.review import ReviewCandidateModel, ReviewRunModel
from patchfrog.persistence.repositories import RepositoryRepository
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
            await conn.execute(text("SELECT 1 FROM review_candidates LIMIT 1"))
    except (OperationalError, ProgrammingError):
        await engine.dispose()
        return None
    return engine


async def test_deleting_a_review_run_cascades_to_candidates_but_not_into_the_context_bundle(
    tmp_path: Path,
) -> None:
    engine = await _postgres_available()
    if engine is None:
        pytest.skip("real PostgreSQL not reachable at localhost:5432 (docker compose up -d postgres)")

    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    full_name = f"bundle-cascade-test/{uuid.uuid4().hex[:8]}"
    repository_id: uuid.UUID | None = None

    try:
        async with session_factory() as session:
            repo_row = await RepositoryRepository().upsert(
                session,
                github_repository_id=uuid.uuid4().int & 0x7FFFFFFFFFFFFFFF,
                owner="bundle-cascade-test",
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
        await service.review_local(
            repository_id=repository_id, root_path=snapshot.root_path, repository_full_name=full_name,
            commit_sha=snapshot.commit_sha, diff_files=diff_files,
        )

        async with session_factory() as session:
            run = (
                await session.execute(select(ReviewRunModel).where(ReviewRunModel.repository_id == repository_id))
            ).scalars().one()
            candidate = (
                await session.execute(
                    select(ReviewCandidateModel).where(ReviewCandidateModel.review_run_id == run.id)
                )
            ).scalars().one()
            assert candidate.context_bundle_id is not None
            bundle_id = candidate.context_bundle_id

            bundle_before = await session.get(ContextBundleModel, bundle_id)
            assert bundle_before is not None

        # Delete the review run -- must cascade to review_candidates (and,
        # transitively, ai_finding_proposals/ai_findings) but must leave
        # the context_bundles row completely untouched.
        async with session_factory() as session:
            run_to_delete = await session.get(ReviewRunModel, run.id)
            assert run_to_delete is not None
            await session.delete(run_to_delete)
            await session.commit()

        async with session_factory() as session:
            remaining_candidates = (
                await session.execute(
                    select(ReviewCandidateModel).where(ReviewCandidateModel.review_run_id == run.id)
                )
            ).scalars().all()
            assert remaining_candidates == []  # cascaded away with the run

            bundle_after = await session.get(ContextBundleModel, bundle_id)
            assert bundle_after is not None  # context bundle survives independently
            assert bundle_after.id == bundle_before.id
            assert bundle_after.commit_sha == bundle_before.commit_sha
    finally:
        if repository_id is not None:
            async with session_factory() as session:
                run_rows = (
                    await session.execute(select(ReviewRunModel).where(ReviewRunModel.repository_id == repository_id))
                ).scalars().all()
                for row in run_rows:
                    await session.delete(row)
                await session.flush()
                await session.execute(
                    text("DELETE FROM context_bundles WHERE repository_id = :id"), {"id": str(repository_id)}
                )
                await session.execute(text("DELETE FROM repository_indexes WHERE repository_id = :id"), {"id": str(repository_id)})
                await session.execute(text("DELETE FROM repositories WHERE id = :id"), {"id": str(repository_id)})
                await session.commit()
        await engine.dispose()
        shutil.rmtree(tmp_path / "repo", ignore_errors=True)
