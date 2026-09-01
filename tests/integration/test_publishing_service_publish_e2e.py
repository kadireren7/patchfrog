"""End-to-end (fake-GitHub) coverage for ReviewPublicationService's
PUBLISH path: real Phase 5 review_run -> planner -> diff mapper ->
persistence -> fake GitHub publish -> GitHub IDs saved -> retry -> no
duplicate. Covers Phase 6 spec sections 46/47."""

from __future__ import annotations

from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from patchfrog.analysis.domain import Severity
from patchfrog.domain.pull_request import PullRequestMetadata
from patchfrog.publishing.config import PublicationConfig
from patchfrog.publishing.domain import ReviewPublicationMode, ReviewPublicationStatus
from patchfrog.publishing.fake_publisher import FakeReviewPublisher
from patchfrog.publishing.marker import find_marker
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


async def test_real_publish_writes_exactly_one_github_review(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    reviewed = await setup_reviewed_pull_request(
        session_factory,
        full_name="test/publish-e2e-1",
        changed_lines=[14, 22],  # can_withdraw + apply_payment_result -- two distinct findings
        response_factory=lambda req: scripted_findings_response([finding_json()]),
        tmp_root=tmp_path,
    )
    assert len(reviewed.findings) >= 1

    publisher = FakeReviewPublisher(
        pull_request=_pr_metadata(number=reviewed.pull_request_number, head_sha=reviewed.commit_sha),
        changed_files=reviewed.changed_files,
    )
    service = ReviewPublicationService(session_factory=session_factory, publisher=publisher)

    result = await service.publish(
        review_run_id=reviewed.review_run_id,
        mode=ReviewPublicationMode.PUBLISH,
        config=PublicationConfig(enabled=True, min_severity=Severity.INFO),
    )

    assert result.status is ReviewPublicationStatus.PUBLISHED
    assert result.github_review_id is not None
    assert result.published_inline == result.planned_inline
    assert len(publisher.publish_calls) == 1

    call = publisher.publish_calls[0]
    assert call.commit_id == reviewed.commit_sha
    assert find_marker(call.body) == result.publication_id


async def test_retrying_the_same_review_run_after_success_is_idempotent(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    reviewed = await setup_reviewed_pull_request(
        session_factory,
        full_name="test/publish-e2e-idempotent",
        changed_lines=[14],
        response_factory=lambda req: scripted_findings_response([finding_json()]),
        tmp_root=tmp_path,
    )

    publisher = FakeReviewPublisher(
        pull_request=_pr_metadata(number=reviewed.pull_request_number, head_sha=reviewed.commit_sha),
        changed_files=reviewed.changed_files,
    )
    service = ReviewPublicationService(session_factory=session_factory, publisher=publisher)
    config = PublicationConfig(enabled=True, min_severity=Severity.INFO)

    first = await service.publish(review_run_id=reviewed.review_run_id, mode=ReviewPublicationMode.PUBLISH, config=config)
    second = await service.publish(review_run_id=reviewed.review_run_id, mode=ReviewPublicationMode.PUBLISH, config=config)

    assert first.status is ReviewPublicationStatus.PUBLISHED
    assert second.status is ReviewPublicationStatus.PUBLISHED
    assert second.reconciled is True
    assert second.publication_id == first.publication_id
    assert second.github_review_id == first.github_review_id
    assert len(publisher.publish_calls) == 1  # no duplicate GitHub write


async def test_post_clean_summary_enabled_publishes_a_real_clean_review(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    """External beta readiness: a genuinely clean review must not look
    like PatchFrog silently failed once a repository opts in -- proven
    as a real GitHub write (FakeReviewPublisher), not just DRY_RUN
    planning (see test_publishing_service_dry_run.py for that half)."""

    reviewed = await setup_reviewed_pull_request(
        session_factory,
        full_name="test/clean-summary-publish",
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
        mode=ReviewPublicationMode.PUBLISH,
        config=PublicationConfig(enabled=True, post_clean_summary=True),
    )

    assert result.status is ReviewPublicationStatus.PUBLISHED
    assert result.published_inline == 0
    assert len(publisher.publish_calls) == 1
    call = publisher.publish_calls[0]
    assert "no publishable findings" in call.body
    assert call.comments == ()


async def test_realistic_fixture_more_findings_than_cap_yields_inline_plus_summary(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    """Realistic PR diff fixture (several functions changed), more
    findings than the inline comment cap, plus one unmappable finding --
    verifies the final review contains the correct inline subset plus
    summary fallback (spec section 47)."""

    findings_payload = [
        finding_json(title="finding-1", start_line=14, end_line=14, severity="critical"),
        finding_json(title="finding-2", start_line=22, end_line=22, severity="high"),
        finding_json(title="finding-3", start_line=39, end_line=39, severity="medium"),
        finding_json(title="finding-4", start_line=52, end_line=52, severity="low"),
    ]
    reviewed = await setup_reviewed_pull_request(
        session_factory,
        full_name="test/publish-e2e-realistic",
        changed_lines=[14, 22, 39, 52],
        response_factory=lambda req: scripted_findings_response(findings_payload),
        tmp_root=tmp_path,
    )
    assert len(reviewed.findings) == 4

    publisher = FakeReviewPublisher(
        pull_request=_pr_metadata(number=reviewed.pull_request_number, head_sha=reviewed.commit_sha),
        changed_files=reviewed.changed_files,
    )
    service = ReviewPublicationService(session_factory=session_factory, publisher=publisher)
    config = PublicationConfig(enabled=True, min_severity=Severity.INFO, max_inline_comments=2)

    result = await service.publish(review_run_id=reviewed.review_run_id, mode=ReviewPublicationMode.PUBLISH, config=config)

    assert result.status is ReviewPublicationStatus.PUBLISHED
    assert result.published_inline == 2  # capped
    assert result.summary_only == 2  # overflow preserved, not dropped
    assert result.omitted == 0

    call = publisher.publish_calls[0]
    assert len(call.comments) == 2
    assert "Additional findings" in call.body  # summary fallback section present
