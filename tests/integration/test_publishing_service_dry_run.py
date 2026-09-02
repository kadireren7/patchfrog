"""End-to-end (fake-GitHub) coverage for ReviewPublicationService's
DRY_RUN path: real Phase 5 review_run + ai_findings -> planner -> no
GitHub write -> persisted plan/comments for audit."""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from patchfrog.domain.pull_request import PullRequestMetadata
from patchfrog.persistence.models.publishing import (
    ReviewPublicationCommentModel,
    ReviewPublicationModel,
)
from patchfrog.publishing.config import PublicationConfig
from patchfrog.publishing.domain import (
    PublicationDisposition,
    ReviewPublicationMode,
    ReviewPublicationStatus,
)
from patchfrog.publishing.fake_publisher import FakeReviewPublisher
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


async def test_dry_run_plans_but_never_writes_to_github(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    reviewed = await setup_reviewed_pull_request(
        session_factory,
        full_name="test/dry-run-1",
        changed_lines=[14],
        response_factory=lambda req: scripted_findings_response([finding_json()]),
        tmp_root=tmp_path,
    )
    assert len(reviewed.findings) == 1

    publisher = FakeReviewPublisher(
        pull_request=_pr_metadata(number=reviewed.pull_request_number, head_sha=reviewed.commit_sha),
        changed_files=reviewed.changed_files,
    )
    service = ReviewPublicationService(session_factory=session_factory, publisher=publisher)

    result = await service.publish(review_run_id=reviewed.review_run_id, mode=ReviewPublicationMode.DRY_RUN)

    assert result.status is ReviewPublicationStatus.DRY_RUN
    assert result.published_inline == 0  # never actually published
    assert result.planned_inline == 1
    assert publisher.publish_calls == []  # the actual GitHub write method was never called

    async with session_factory() as session:
        pub = await session.get(ReviewPublicationModel, result.publication_id)
        assert pub is not None
        assert pub.mode == ReviewPublicationMode.DRY_RUN
        assert pub.github_review_id is None

        comments = (
            await session.execute(
                select(ReviewPublicationCommentModel).where(
                    ReviewPublicationCommentModel.review_publication_id == pub.id
                )
            )
        ).scalars().all()
        assert len(comments) == 1
        assert comments[0].disposition == PublicationDisposition.INLINE
        assert comments[0].github_comment_id is None


async def test_dry_run_with_no_findings_is_skipped(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    reviewed = await setup_reviewed_pull_request(
        session_factory,
        full_name="test/dry-run-no-findings",
        changed_lines=[14],
        response_factory=lambda req: scripted_findings_response([]),
        tmp_root=tmp_path,
    )
    assert reviewed.findings == []

    publisher = FakeReviewPublisher(
        pull_request=_pr_metadata(number=reviewed.pull_request_number, head_sha=reviewed.commit_sha),
        changed_files=reviewed.changed_files,
    )
    service = ReviewPublicationService(session_factory=session_factory, publisher=publisher)
    result = await service.publish(review_run_id=reviewed.review_run_id, mode=ReviewPublicationMode.DRY_RUN)

    assert result.status is ReviewPublicationStatus.SKIPPED_NO_FINDINGS
    assert publisher.publish_calls == []


async def test_dry_run_with_post_clean_summary_enabled_plans_a_clean_review(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    reviewed = await setup_reviewed_pull_request(
        session_factory,
        full_name="test/clean-summary-dry-run",
        changed_lines=[14],
        response_factory=lambda req: scripted_findings_response([]),
        tmp_root=tmp_path,
    )
    assert reviewed.findings == []

    publisher = FakeReviewPublisher(
        pull_request=_pr_metadata(number=reviewed.pull_request_number, head_sha=reviewed.commit_sha),
        changed_files=reviewed.changed_files,
    )
    service = ReviewPublicationService(session_factory=session_factory, publisher=publisher)
    result = await service.publish(
        review_run_id=reviewed.review_run_id,
        mode=ReviewPublicationMode.DRY_RUN,
        config=PublicationConfig(post_clean_summary=True),
    )

    assert result.status is ReviewPublicationStatus.DRY_RUN
    assert result.published_inline == 0
    assert result.planned_inline == 0
    assert publisher.publish_calls == []  # DRY_RUN never writes


async def test_dry_run_unmappable_finding_preserved_in_summary(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    """A finding whose file is no longer part of the *current* GitHub
    diff (e.g. the live PR diff has since changed) must never be silently
    dropped -- it falls back to summary-only (section 9 of the Phase 6
    spec)."""

    reviewed = await setup_reviewed_pull_request(
        session_factory,
        full_name="test/dry-run-unmappable",
        changed_lines=[14],
        response_factory=lambda req: scripted_findings_response([finding_json()]),
        tmp_root=tmp_path,
    )
    assert len(reviewed.findings) == 1

    publisher = FakeReviewPublisher(
        pull_request=_pr_metadata(number=reviewed.pull_request_number, head_sha=reviewed.commit_sha),
        changed_files=[],  # billing.py no longer appears in the live GitHub diff
    )
    service = ReviewPublicationService(session_factory=session_factory, publisher=publisher)
    result = await service.publish(review_run_id=reviewed.review_run_id, mode=ReviewPublicationMode.DRY_RUN)

    assert result.status is ReviewPublicationStatus.DRY_RUN
    assert result.planned_inline == 0
    assert result.summary_only == 1
