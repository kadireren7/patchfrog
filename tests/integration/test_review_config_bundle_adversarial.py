"""Adversarial mini-audit for the config-parity and context_bundle_id
fixes: concurrent duplicate delivery of a malformed-config failure, and
retry-after-partial-failure through the *production-shaped* flow (resolve
config -> build providers -> review), not just the service in isolation.
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
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from patchfrog.diff.models import DiffFile, DiffHunk, DiffLine, DiffLineType
from patchfrog.indexing.service import RepositoryIndexingService
from patchfrog.persistence.models.review import ReviewRunModel
from patchfrog.persistence.repositories import RepositoryRepository
from patchfrog.review.config import MalformedReviewConfigError
from patchfrog.review.config_resolution import resolve_repository_review_config
from patchfrog.review.domain import ReviewRunStatus
from patchfrog.review.provider import ProviderFatalError, ProviderRequest
from patchfrog.review.providers.fake import FakeLLMProvider, ScriptedResponse
from patchfrog.review.service import PullRequestReviewService, persist_malformed_config_failure
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


async def test_concurrent_duplicate_malformed_config_delivery_never_crashes(tmp_path: Path) -> None:
    """Two 'concurrent Celery deliveries' both hitting the exact same
    malformed .patchfrog.yml for the same commit must both resolve
    cleanly to a persisted failure record, serialized by the same
    pg_advisory_xact_lock every other review-run identity uses -- no
    crash, no unhandled exception, no partial/duplicate row corruption."""

    engine = await _postgres_available()
    if engine is None:
        pytest.skip("real PostgreSQL not reachable at localhost:5432 (docker compose up -d postgres)")

    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    full_name = f"malformed-concurrency-test/{uuid.uuid4().hex[:8]}"
    repository_id: uuid.UUID | None = None

    try:
        async with session_factory() as session:
            repo_row = await RepositoryRepository().upsert(
                session,
                github_repository_id=uuid.uuid4().int & 0x7FFFFFFFFFFFFFFF,
                owner="malformed-concurrency-test",
                name=full_name,
                full_name=full_name,
                installation_id=0,
            )
            await session.commit()
            repository_id = repo_row.id

        snapshot = materialize_fixture_repo(tmp_path / "repo", "ai_review_python", full_name=full_name)
        (snapshot.root_path / ".patchfrog.yml").write_text("review: [not a mapping\n")
        await RepositoryIndexingService(session_factory=session_factory).index_local_repository(
            repository_id=repository_id, root_path=snapshot.root_path, repository_full_name=full_name
        )

        async def _attempt() -> ReviewRunModel:
            try:
                await resolve_repository_review_config(
                    local=True, commit_sha=snapshot.commit_sha, repository_full_name=full_name,
                    root_path=snapshot.root_path,
                )
                raise AssertionError("expected MalformedReviewConfigError")
            except MalformedReviewConfigError as exc:
                return await persist_malformed_config_failure(
                    session_factory, repository_id=repository_id, commit_sha=snapshot.commit_sha,
                    pull_request_id=None, exc=exc,
                )

        results = await asyncio.gather(_attempt(), _attempt(), return_exceptions=True)
        for result in results:
            if isinstance(result, BaseException):
                raise AssertionError(f"concurrent malformed-config handling raised unexpectedly: {result!r}")

        async with session_factory() as session:
            rows = (
                await session.execute(select(ReviewRunModel).where(ReviewRunModel.repository_id == repository_id))
            ).scalars().all()
        assert len(rows) == 2  # each attempt is its own historical record (see persist_malformed_config_failure)
        assert all(r.status == ReviewRunStatus.FAILED for r in rows)
        assert len({r.config_fingerprint for r in rows}) == 1  # same bad content -> same identity
    finally:
        if repository_id is not None:
            async with session_factory() as session:
                run_rows = (
                    await session.execute(select(ReviewRunModel).where(ReviewRunModel.repository_id == repository_id))
                ).scalars().all()
                for row in run_rows:
                    await session.delete(row)
                await session.flush()
                await session.execute(text("DELETE FROM repository_indexes WHERE repository_id = :id"), {"id": str(repository_id)})
                await session.execute(text("DELETE FROM repositories WHERE id = :id"), {"id": str(repository_id)})
                await session.commit()
        await engine.dispose()
        shutil.rmtree(tmp_path / "repo", ignore_errors=True)


async def _setup(
    session_factory: async_sessionmaker[AsyncSession], *, full_name: str
) -> tuple[uuid.UUID, str, Path]:
    async with session_factory() as session:
        row = await RepositoryRepository().upsert(
            session, github_repository_id=abs(hash(full_name)) % (2**62),
            owner="test", name=full_name.split("/")[-1], full_name=full_name, installation_id=0,
        )
        await session.commit()
        repository_id = row.id

    root = Path("/tmp") / f"pf-ai-review-retry-{uuid.uuid4().hex[:8]}"
    snapshot = materialize_fixture_repo(root, "ai_review_python", full_name=full_name)
    await RepositoryIndexingService(session_factory=session_factory).index_local_repository(
        repository_id=repository_id, root_path=snapshot.root_path, repository_full_name=full_name
    )
    return repository_id, snapshot.commit_sha, snapshot.root_path


async def test_retry_after_partial_failure_through_the_production_shaped_flow(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Resolve config the same way the CLI/Celery task does, hit a
    partial failure (one candidate's provider call fails), then retry
    with the identical resolved config -- the retry must not reuse the
    partial run (only 'succeeded' identities are ever reused) and must
    itself be able to fully succeed."""

    repository_id, commit_sha, root_path = await _setup(session_factory, full_name="test/retry-partial")
    diff_files = [_diff_marking_lines("src/billing.py", [14, 22])]  # can_withdraw + apply_payment_result

    config = await resolve_repository_review_config(
        local=True, commit_sha=commit_sha, repository_full_name="test/retry-partial", root_path=root_path
    )

    def failing_factory(request: ProviderRequest) -> ScriptedResponse | Exception:
        if "Review target: `apply_payment_result`" in request.user_prompt:
            return ProviderFatalError("simulated transient outage")
        return ScriptedResponse(raw_json=json.dumps({"findings": []}))

    failing_provider = FakeLLMProvider(response_factory=failing_factory)
    first_service = PullRequestReviewService(session_factory=session_factory, reviewer_provider=failing_provider)
    first = await first_service.review_local(
        repository_id=repository_id, root_path=root_path, repository_full_name="test/retry-partial",
        commit_sha=commit_sha, diff_files=diff_files, config=config,
    )
    assert first.status == ReviewRunStatus.PARTIAL
    assert first.reused_existing_run is False

    healthy_provider = FakeLLMProvider(
        response_factory=lambda req: ScriptedResponse(raw_json=json.dumps({"findings": []}))
    )
    retry_service = PullRequestReviewService(session_factory=session_factory, reviewer_provider=healthy_provider)
    retry = await retry_service.review_local(
        repository_id=repository_id, root_path=root_path, repository_full_name="test/retry-partial",
        commit_sha=commit_sha, diff_files=diff_files, config=config,
    )

    assert retry.status == ReviewRunStatus.SUCCEEDED
    assert retry.reused_existing_run is False  # a PARTIAL run is never treated as canonical
    assert retry.candidates_failed == 0

    async with session_factory() as session:
        runs = (
            await session.execute(select(ReviewRunModel).where(ReviewRunModel.repository_id == repository_id))
        ).scalars().all()
    statuses = sorted(r.status.value for r in runs)
    assert statuses == ["partial", "succeeded"]
