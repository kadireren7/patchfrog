"""Concurrent-publish regression test against a real PostgreSQL database.

Mirrors ``tests/integration/test_review_run_concurrency.py`` exactly --
SQLite cannot reproduce this race (no advisory locks, no real MVCC).
Skips (rather than failing) if a migrated Postgres isn't reachable at
``localhost:5432``.

Guards against: two workers concurrently attempting to publish the exact
same ``(review_run_id, mode=PUBLISH)`` identity both racing past the
idempotency check and both writing a GitHub review. Phase 6 spec section
29/42.
"""

from __future__ import annotations

import asyncio
import shutil
import uuid
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.exc import OperationalError, ProgrammingError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from patchfrog.domain.pull_request import PullRequestMetadata
from patchfrog.publishing.config import PublicationConfig
from patchfrog.publishing.domain import ReviewPublicationMode, ReviewPublicationStatus
from patchfrog.publishing.fake_publisher import FakeReviewPublisher
from patchfrog.publishing.service import ReviewPublicationService
from tests.support.publishing import (
    finding_json,
    scripted_findings_response,
    setup_reviewed_pull_request,
)

_POSTGRES_URL = "postgresql+asyncpg://patchfrog:patchfrog@localhost:5432/patchfrog"


async def _postgres_available() -> AsyncEngine | None:
    engine = create_async_engine(_POSTGRES_URL)
    try:
        async with engine.begin() as conn:
            await conn.execute(text("SELECT 1 FROM review_publications LIMIT 1"))
    except (OperationalError, ProgrammingError):
        await engine.dispose()
        return None
    return engine


def _pr_metadata(*, number: int, head_sha: str) -> PullRequestMetadata:
    return PullRequestMetadata(
        number=number, title="t", body=None, author="a", base_branch="main", head_branch="feature",
        base_sha="0" * 40, head_sha=head_sha, html_url="https://github.com/test/repo/pull/1", state="open",
    )


async def test_two_concurrent_publish_attempts_write_exactly_one_github_review(tmp_path: Path) -> None:
    engine = await _postgres_available()
    if engine is None:
        pytest.skip("real PostgreSQL not reachable at localhost:5432 (docker compose up -d postgres)")

    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    repository_id = None

    try:
        reviewed = await setup_reviewed_pull_request(
            session_factory,
            full_name=f"concurrency-test/publish-{uuid.uuid4().hex[:8]}",
            changed_lines=[14],
            response_factory=lambda req: scripted_findings_response([finding_json()]),
            tmp_root=tmp_path,
        )
        repository_id = reviewed.repository_id

        # One FakeReviewPublisher instance shared by both concurrent
        # attempts -- simulates the same real GitHub backend both workers
        # would hit.
        publisher = FakeReviewPublisher(
            pull_request=_pr_metadata(number=reviewed.pull_request_number, head_sha=reviewed.commit_sha),
            changed_files=reviewed.changed_files,
        )
        service_a = ReviewPublicationService(session_factory=session_factory, publisher=publisher)
        service_b = ReviewPublicationService(session_factory=session_factory, publisher=publisher)
        config = PublicationConfig(enabled=True)

        results = await asyncio.gather(
            service_a.publish(review_run_id=reviewed.review_run_id, mode=ReviewPublicationMode.PUBLISH, config=config),
            service_b.publish(review_run_id=reviewed.review_run_id, mode=ReviewPublicationMode.PUBLISH, config=config),
            return_exceptions=True,
        )
        for result in results:
            if isinstance(result, BaseException):
                raise AssertionError(f"concurrent publish raised unexpectedly: {result!r}")

        assert len(publisher.publish_calls) == 1, "exactly one real GitHub write, never two"
        statuses = {r.status for r in results}  # type: ignore[union-attr]
        assert statuses <= {ReviewPublicationStatus.PUBLISHED, ReviewPublicationStatus.PUBLISHING}

        published = [r for r in results if getattr(r, "status", None) == ReviewPublicationStatus.PUBLISHED]
        assert len(published) >= 1
    finally:
        if repository_id is not None:
            async with session_factory() as session:
                await session.execute(
                    text(
                        "DELETE FROM review_publication_comments WHERE review_publication_id IN "
                        "(SELECT id FROM review_publications WHERE repository_id = :rid)"
                    ),
                    {"rid": str(repository_id)},
                )
                await session.execute(text("DELETE FROM review_publications WHERE repository_id = :rid"), {"rid": str(repository_id)})
                await session.execute(text("DELETE FROM ai_findings WHERE review_run_id IN (SELECT id FROM review_runs WHERE repository_id = :rid)"), {"rid": str(repository_id)})
                await session.execute(text("DELETE FROM ai_finding_proposals WHERE review_run_id IN (SELECT id FROM review_runs WHERE repository_id = :rid)"), {"rid": str(repository_id)})
                await session.execute(text("DELETE FROM review_candidates WHERE review_run_id IN (SELECT id FROM review_runs WHERE repository_id = :rid)"), {"rid": str(repository_id)})
                await session.execute(text("DELETE FROM review_runs WHERE repository_id = :rid"), {"rid": str(repository_id)})
                await session.execute(text("DELETE FROM context_bundles WHERE repository_id = :rid"), {"rid": str(repository_id)})
                await session.execute(text("DELETE FROM pull_requests WHERE repository_id = :rid"), {"rid": str(repository_id)})
                await session.execute(text("DELETE FROM repository_indexes WHERE repository_id = :rid"), {"rid": str(repository_id)})
                await session.execute(text("DELETE FROM repositories WHERE id = :rid"), {"rid": str(repository_id)})
                await session.commit()
        await engine.dispose()
        shutil.rmtree(tmp_path / "repo", ignore_errors=True)
