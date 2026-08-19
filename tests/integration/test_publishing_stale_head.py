"""Mandatory stale-head protection coverage (Phase 6 spec section 43):
review generated at SHA A, current PR SHA B -> STALE, zero GitHub
writes."""

from __future__ import annotations

from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

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

_DIFFERENT_HEAD_SHA = "f" * 40


def _pr_metadata(*, number: int, head_sha: str) -> PullRequestMetadata:
    return PullRequestMetadata(
        number=number, title="t", body=None, author="a", base_branch="main", head_branch="feature",
        base_sha="0" * 40, head_sha=head_sha, html_url="https://github.com/test/repo/pull/1", state="open",
    )


async def test_stale_head_blocks_publish_with_zero_github_writes(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    reviewed = await setup_reviewed_pull_request(
        session_factory,
        full_name="test/stale-head-publish",
        changed_lines=[14],
        response_factory=lambda req: scripted_findings_response([finding_json()]),
        tmp_root=tmp_path,
    )
    assert reviewed.commit_sha != _DIFFERENT_HEAD_SHA

    # Simulate the PR having received a new commit since the review ran --
    # the live "current head" GitHub reports no longer matches what was
    # reviewed.
    publisher = FakeReviewPublisher(
        pull_request=_pr_metadata(number=reviewed.pull_request_number, head_sha=_DIFFERENT_HEAD_SHA),
        changed_files=reviewed.changed_files,
    )
    service = ReviewPublicationService(session_factory=session_factory, publisher=publisher)

    result = await service.publish(
        review_run_id=reviewed.review_run_id,
        mode=ReviewPublicationMode.PUBLISH,
        config=PublicationConfig(enabled=True),
    )

    assert result.status is ReviewPublicationStatus.STALE
    assert result.published_inline == 0
    assert result.github_review_id is None
    assert publisher.publish_calls == []  # zero GitHub writes, mandatory


async def test_stale_head_blocks_dry_run_too(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    """Dry-run must detect and report staleness even though it never
    writes -- planning must reflect reality, not a stale snapshot."""

    reviewed = await setup_reviewed_pull_request(
        session_factory,
        full_name="test/stale-head-dry-run",
        changed_lines=[14],
        response_factory=lambda req: scripted_findings_response([finding_json()]),
        tmp_root=tmp_path,
    )

    publisher = FakeReviewPublisher(
        pull_request=_pr_metadata(number=reviewed.pull_request_number, head_sha=_DIFFERENT_HEAD_SHA),
        changed_files=reviewed.changed_files,
    )
    service = ReviewPublicationService(session_factory=session_factory, publisher=publisher)

    result = await service.publish(review_run_id=reviewed.review_run_id, mode=ReviewPublicationMode.DRY_RUN)

    assert result.status is ReviewPublicationStatus.STALE
    assert publisher.publish_calls == []


async def test_race_before_final_write_blocks_publish(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    """The PR receives a new commit between the initial staleness check
    and the final pre-write re-check (spec section 28) -- must still be
    caught, with zero GitHub writes."""

    reviewed = await setup_reviewed_pull_request(
        session_factory,
        full_name="test/stale-head-race",
        changed_lines=[14],
        response_factory=lambda req: scripted_findings_response([finding_json()]),
        tmp_root=tmp_path,
    )

    # First get_head_sha call (initial check) returns the correct SHA,
    # matching the review -- planning proceeds. Second call (the final
    # pre-write race check) returns a different SHA, simulating a new
    # commit landing in between.
    publisher = FakeReviewPublisher(
        pull_request=_pr_metadata(number=reviewed.pull_request_number, head_sha=reviewed.commit_sha),
        changed_files=reviewed.changed_files,
        head_sha_sequence=[reviewed.commit_sha, _DIFFERENT_HEAD_SHA],
    )
    service = ReviewPublicationService(session_factory=session_factory, publisher=publisher)

    result = await service.publish(
        review_run_id=reviewed.review_run_id,
        mode=ReviewPublicationMode.PUBLISH,
        config=PublicationConfig(enabled=True),
    )

    assert result.status is ReviewPublicationStatus.STALE
    assert publisher.publish_calls == []
    assert publisher.head_sha_call_count == 2
