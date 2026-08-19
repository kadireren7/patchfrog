"""Idempotency/reconciliation coverage (Phase 6 spec section 42): publish
same plan twice -> one GitHub review; DB state lost but GitHub marker
exists -> reconcile; GitHub write fails before completion -> retry safe.

True concurrent-worker coverage (two workers racing for real) lives in
``tests/integration/test_publishing_concurrency.py`` against real
Postgres -- SQLite's advisory lock is a no-op (see
``ReviewPublicationRepository._lock_identity``), so it cannot reproduce
that race."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from patchfrog.domain.github_review import GitHubReviewEvent
from patchfrog.domain.pull_request import PullRequestMetadata, PullRequestRef
from patchfrog.github.errors import GitHubServerError
from patchfrog.persistence.models.publishing import ReviewPublicationModel
from patchfrog.publishing.config import PublicationConfig
from patchfrog.publishing.domain import ReviewPublicationMode, ReviewPublicationStatus
from patchfrog.publishing.fake_publisher import FakeReviewPublisher
from patchfrog.publishing.marker import render_marker
from patchfrog.publishing.service import ReviewPublicationService
from tests.support.publishing import (
    finding_json,
    scripted_findings_response,
    setup_reviewed_pull_request,
)


def _pr_metadata(*, number: int, head_sha: str) -> PullRequestMetadata:
    return PullRequestMetadata(
        number=number, title="t", body=None, author="a", base_branch="main", head_branch="feature",
        base_sha="0" * 40, head_sha=head_sha, html_url="https://github.com/test/repo/pull/1", state="open",
    )


async def test_reconciliation_recovers_when_db_state_lost_but_github_marker_exists(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    """Simulates a crash: a PUBLISHING row exists (durable pre-write
    marker), the GitHub write actually succeeded, but the process died
    before the DB commit that would have marked it PUBLISHED. A later
    attempt must recover by finding the marker on GitHub -- not write a
    duplicate."""

    reviewed = await setup_reviewed_pull_request(
        session_factory,
        full_name="test/idempotent-reconcile",
        changed_lines=[14],
        response_factory=lambda req: scripted_findings_response([finding_json()]),
        tmp_root=tmp_path,
    )

    publisher = FakeReviewPublisher(
        pull_request=_pr_metadata(number=reviewed.pull_request_number, head_sha=reviewed.commit_sha),
        changed_files=reviewed.changed_files,
    )

    # Manually create the abandoned PUBLISHING row exactly as
    # get_or_create_attempt would have, but stamped well past the
    # in-flight grace period.
    async with session_factory() as session:
        abandoned = ReviewPublicationModel(
            review_run_id=reviewed.review_run_id,
            repository_id=reviewed.repository_id,
            pull_request_id=reviewed.pull_request_id,
            pull_request_number=reviewed.pull_request_number,
            base_sha="0" * 40,
            head_sha=reviewed.commit_sha,
            mode=ReviewPublicationMode.PUBLISH,
            status=ReviewPublicationStatus.PUBLISHING,
            started_at=datetime.now(UTC) - timedelta(minutes=30),
        )
        session.add(abandoned)
        await session.commit()
        publication_id = abandoned.id

    # And GitHub already has the review, carrying that exact publication's marker.
    await publisher.publish_review(
        ref=PullRequestRef(owner="t", repository="r", number=reviewed.pull_request_number),
        commit_id=reviewed.commit_sha,
        body=f"## PatchFrog review summary\n\n{render_marker(publication_id)}",
        event=GitHubReviewEvent.COMMENT,
        comments=[],
    )
    publisher.publish_calls.clear()  # that call above was test setup, not the attempt under test

    service = ReviewPublicationService(session_factory=session_factory, publisher=publisher)
    result = await service.publish(
        review_run_id=reviewed.review_run_id, mode=ReviewPublicationMode.PUBLISH, config=PublicationConfig(enabled=True)
    )

    assert result.status is ReviewPublicationStatus.PUBLISHED
    assert result.reconciled is True
    assert result.publication_id == publication_id
    assert publisher.publish_calls == []  # recovered via reconciliation -- no new write


async def test_abandoned_in_flight_with_no_github_marker_allows_a_fresh_safe_retry(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    """DB says a publish was in flight, but GitHub never actually
    received it (crashed before the write) -- a retry must supersede the
    abandoned row and publish for real, exactly once."""

    reviewed = await setup_reviewed_pull_request(
        session_factory,
        full_name="test/idempotent-abandoned-no-marker",
        changed_lines=[14],
        response_factory=lambda req: scripted_findings_response([finding_json()]),
        tmp_root=tmp_path,
    )

    async with session_factory() as session:
        abandoned = ReviewPublicationModel(
            review_run_id=reviewed.review_run_id,
            repository_id=reviewed.repository_id,
            pull_request_id=reviewed.pull_request_id,
            pull_request_number=reviewed.pull_request_number,
            base_sha="0" * 40,
            head_sha=reviewed.commit_sha,
            mode=ReviewPublicationMode.PUBLISH,
            status=ReviewPublicationStatus.PUBLISHING,
            started_at=datetime.now(UTC) - timedelta(minutes=30),
        )
        session.add(abandoned)
        await session.commit()

    publisher = FakeReviewPublisher(
        pull_request=_pr_metadata(number=reviewed.pull_request_number, head_sha=reviewed.commit_sha),
        changed_files=reviewed.changed_files,
    )
    service = ReviewPublicationService(session_factory=session_factory, publisher=publisher)
    result = await service.publish(
        review_run_id=reviewed.review_run_id, mode=ReviewPublicationMode.PUBLISH, config=PublicationConfig(enabled=True)
    )

    assert result.status is ReviewPublicationStatus.PUBLISHED
    assert result.reconciled is False
    assert len(publisher.publish_calls) == 1


async def test_github_write_failure_is_retry_safe_no_duplicate(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    reviewed = await setup_reviewed_pull_request(
        session_factory,
        full_name="test/idempotent-retry-safe",
        changed_lines=[14],
        response_factory=lambda req: scripted_findings_response([finding_json()]),
        tmp_root=tmp_path,
    )

    failing_publisher = FakeReviewPublisher(
        pull_request=_pr_metadata(number=reviewed.pull_request_number, head_sha=reviewed.commit_sha),
        changed_files=reviewed.changed_files,
        publish_exception=GitHubServerError("simulated transient 503"),
    )
    failing_service = ReviewPublicationService(session_factory=session_factory, publisher=failing_publisher)
    config = PublicationConfig(enabled=True)

    first = await failing_service.publish(review_run_id=reviewed.review_run_id, mode=ReviewPublicationMode.PUBLISH, config=config)
    assert first.status is ReviewPublicationStatus.FAILED
    assert len(failing_publisher.publish_calls) == 1  # attempted, but failed

    healthy_publisher = FakeReviewPublisher(
        pull_request=_pr_metadata(number=reviewed.pull_request_number, head_sha=reviewed.commit_sha),
        changed_files=reviewed.changed_files,
    )
    retry_service = ReviewPublicationService(session_factory=session_factory, publisher=healthy_publisher)
    second = await retry_service.publish(review_run_id=reviewed.review_run_id, mode=ReviewPublicationMode.PUBLISH, config=config)

    assert second.status is ReviewPublicationStatus.PUBLISHED
    assert len(healthy_publisher.publish_calls) == 1
    assert first.publication_id != second.publication_id  # each attempt is its own row
