"""Coverage for the malformed-``.patchfrog.yml`` failure path: a bad
committed config must produce a clear, queryable ``review_runs`` row with
``status=failed`` (not a silently-defaulted run), and that failure must
never be attributable to the wrong commit."""

from __future__ import annotations

import uuid
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from patchfrog.indexing.service import RepositoryIndexingService
from patchfrog.persistence.models.review import ReviewRunModel
from patchfrog.persistence.repositories import RepositoryRepository
from patchfrog.review.config import MalformedReviewConfigError
from patchfrog.review.config_resolution import resolve_repository_review_config
from patchfrog.review.domain import ReviewRunStatus
from patchfrog.review.service import StaleReviewIndexError, persist_malformed_config_failure
from tests.support.git_repo import materialize_fixture_repo


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

    root = Path("/tmp") / f"pf-ai-review-malformed-{uuid.uuid4().hex[:8]}"
    snapshot = materialize_fixture_repo(root, "ai_review_python", full_name=full_name)
    (snapshot.root_path / ".patchfrog.yml").write_text("review: [this is not a mapping\n")
    await RepositoryIndexingService(session_factory=session_factory).index_local_repository(
        repository_id=repository_id, root_path=snapshot.root_path, repository_full_name=full_name
    )
    return repository_id, snapshot.commit_sha, snapshot.root_path


async def test_malformed_config_persists_a_failed_run_row(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    repository_id, commit_sha, root_path = await _setup(session_factory, full_name="test/malformed-config-1")

    try:
        await resolve_repository_review_config(
            local=True, commit_sha=commit_sha, repository_full_name="test/malformed-config-1", root_path=root_path
        )
        raise AssertionError("expected MalformedReviewConfigError")
    except MalformedReviewConfigError as exc:
        run = await persist_malformed_config_failure(
            session_factory, repository_id=repository_id, commit_sha=commit_sha, pull_request_id=None, exc=exc
        )

    assert run.status == ReviewRunStatus.FAILED
    assert run.error_message is not None
    assert "malformed" in run.error_message.lower()
    assert run.reviewer_provider == "unresolved"
    assert run.reviewer_model == "unresolved"

    async with session_factory() as session:
        rows = (
            await session.execute(select(ReviewRunModel).where(ReviewRunModel.repository_id == repository_id))
        ).scalars().all()
    assert len(rows) == 1
    assert rows[0].id == run.id


async def test_repeated_identical_malformed_content_creates_related_but_distinct_failure_rows(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A malformed-config identity never reaches 'succeeded', so unlike a
    normal identity it cannot converge to a single canonical row across
    retries -- each attempt is its own historical record (same behavior
    any other run-level exception already has). Both rows must still
    share the *same* config_fingerprint (content-addressed identity)."""

    repository_id, commit_sha, root_path = await _setup(session_factory, full_name="test/malformed-config-2")

    async def _fail_once() -> ReviewRunModel:
        try:
            await resolve_repository_review_config(
                local=True, commit_sha=commit_sha, repository_full_name="test/malformed-config-2", root_path=root_path
            )
            raise AssertionError("expected MalformedReviewConfigError")
        except MalformedReviewConfigError as exc:
            return await persist_malformed_config_failure(
                session_factory, repository_id=repository_id, commit_sha=commit_sha, pull_request_id=None, exc=exc
            )

    first = await _fail_once()
    second = await _fail_once()

    assert first.id != second.id
    assert first.config_fingerprint == second.config_fingerprint
    assert first.status == second.status == ReviewRunStatus.FAILED


async def test_fixing_the_config_produces_a_distinct_failure_identity(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    repository_id, commit_sha, root_path = await _setup(session_factory, full_name="test/malformed-config-3")

    try:
        await resolve_repository_review_config(
            local=True, commit_sha=commit_sha, repository_full_name="test/malformed-config-3", root_path=root_path
        )
        raise AssertionError("expected MalformedReviewConfigError")
    except MalformedReviewConfigError as exc:
        first = await persist_malformed_config_failure(
            session_factory, repository_id=repository_id, commit_sha=commit_sha, pull_request_id=None, exc=exc
        )

    # A *different* malformed content (still bad, but not identical) --
    # simulates someone editing the file and getting it wrong differently.
    (root_path / ".patchfrog.yml").write_text("review: {still not valid yaml [ \n")
    try:
        await resolve_repository_review_config(
            local=True, commit_sha=commit_sha, repository_full_name="test/malformed-config-3", root_path=root_path
        )
        raise AssertionError("expected MalformedReviewConfigError")
    except MalformedReviewConfigError as exc:
        second = await persist_malformed_config_failure(
            session_factory, repository_id=repository_id, commit_sha=commit_sha, pull_request_id=None, exc=exc
        )

    assert first.config_fingerprint != second.config_fingerprint


async def test_malformed_config_on_wrong_commit_raises_stale_index_not_a_false_failure(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A malformed config must never be attributed to the wrong commit --
    if the repository index doesn't match the commit being reviewed, that
    (StaleReviewIndexError) must surface first, and no review_runs row
    should be created at all."""

    repository_id, _commit_sha, _root_path = await _setup(session_factory, full_name="test/malformed-config-4")

    fake_exc = MalformedReviewConfigError("synthetic", path=Path("/dev/null"), raw_text="synthetic")
    try:
        await persist_malformed_config_failure(
            session_factory,
            repository_id=repository_id,
            commit_sha="0" * 40,  # not the indexed commit
            pull_request_id=None,
            exc=fake_exc,
        )
        raise AssertionError("expected StaleReviewIndexError")
    except StaleReviewIndexError:
        pass

    async with session_factory() as session:
        rows = (
            await session.execute(select(ReviewRunModel).where(ReviewRunModel.repository_id == repository_id))
        ).scalars().all()
    assert rows == []
