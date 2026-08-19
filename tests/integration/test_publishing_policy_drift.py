"""Coverage that changing publication policy (min_severity,
max_inline_comments, or PatchFrog's own comment-format/engine version)
actually changes production identity end to end -- an old-policy
publication must never suppress a new-policy one, and identical policy
must safely reuse the same canonical row. This is the
``publication_policy_fingerprint`` half of a publication's canonical
identity (``review_run_id``, ``mode``, ``publication_policy_fingerprint``)."""

from __future__ import annotations

from pathlib import Path
from unittest import mock

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from patchfrog.analysis.domain import Severity
from patchfrog.domain.pull_request import PullRequestMetadata
from patchfrog.persistence.models.publishing import ReviewPublicationModel
from patchfrog.publishing import config as config_module
from patchfrog.publishing.config import PublicationConfig
from patchfrog.publishing.domain import ReviewPublicationMode, ReviewPublicationStatus
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


async def _published_rows(
    session_factory: async_sessionmaker[AsyncSession], review_run_id: object
) -> list[ReviewPublicationModel]:
    async with session_factory() as session:
        rows = (
            await session.execute(select(ReviewPublicationModel).where(ReviewPublicationModel.review_run_id == review_run_id))
        ).scalars().all()
    return list(rows)


async def test_identical_policy_reuses_the_same_canonical_publication(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    reviewed = await setup_reviewed_pull_request(
        session_factory, full_name="test/policy-drift-identical", changed_lines=[14],
        response_factory=lambda req: scripted_findings_response([finding_json()]), tmp_root=tmp_path,
    )
    publisher = FakeReviewPublisher(
        pull_request=_pr_metadata(number=reviewed.pull_request_number, head_sha=reviewed.commit_sha),
        changed_files=reviewed.changed_files,
    )
    service = ReviewPublicationService(session_factory=session_factory, publisher=publisher)
    config = PublicationConfig(enabled=True, min_severity=Severity.INFO)

    first = await service.publish(review_run_id=reviewed.review_run_id, mode=ReviewPublicationMode.PUBLISH, config=config)
    # A second call with a *new but field-for-field identical* config object.
    same_config = PublicationConfig(enabled=True, min_severity=Severity.INFO)
    second = await service.publish(review_run_id=reviewed.review_run_id, mode=ReviewPublicationMode.PUBLISH, config=same_config)

    assert first.status is ReviewPublicationStatus.PUBLISHED
    assert second.status is ReviewPublicationStatus.PUBLISHED
    assert second.reconciled is True
    assert second.publication_id == first.publication_id
    assert len(publisher.publish_calls) == 1  # no duplicate GitHub write

    rows = await _published_rows(session_factory, reviewed.review_run_id)
    published = [r for r in rows if r.status == ReviewPublicationStatus.PUBLISHED]
    assert len(published) == 1


async def test_min_severity_change_produces_a_distinct_publication_never_suppressed(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    reviewed = await setup_reviewed_pull_request(
        session_factory, full_name="test/policy-drift-severity", changed_lines=[14],
        response_factory=lambda req: scripted_findings_response([finding_json(severity="critical")]),
        tmp_root=tmp_path,
    )
    publisher = FakeReviewPublisher(
        pull_request=_pr_metadata(number=reviewed.pull_request_number, head_sha=reviewed.commit_sha),
        changed_files=reviewed.changed_files,
    )
    service = ReviewPublicationService(session_factory=session_factory, publisher=publisher)

    old_policy = await service.publish(
        review_run_id=reviewed.review_run_id, mode=ReviewPublicationMode.PUBLISH,
        config=PublicationConfig(enabled=True, min_severity=Severity.LOW),
    )
    new_policy = await service.publish(
        review_run_id=reviewed.review_run_id, mode=ReviewPublicationMode.PUBLISH,
        config=PublicationConfig(enabled=True, min_severity=Severity.HIGH),
    )

    # Old-policy publication must never suppress/reconcile-away the new one.
    assert old_policy.status is ReviewPublicationStatus.PUBLISHED
    assert new_policy.status is ReviewPublicationStatus.PUBLISHED
    assert new_policy.reconciled is False
    assert new_policy.publication_id != old_policy.publication_id
    assert len(publisher.publish_calls) == 2  # two genuinely distinct GitHub writes

    rows = await _published_rows(session_factory, reviewed.review_run_id)
    published = [r for r in rows if r.status == ReviewPublicationStatus.PUBLISHED]
    assert len(published) == 2
    assert len({r.publication_policy_fingerprint for r in published}) == 2


async def test_max_inline_comments_change_produces_a_distinct_publication(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    reviewed = await setup_reviewed_pull_request(
        session_factory, full_name="test/policy-drift-cap", changed_lines=[14, 22],
        response_factory=lambda req: scripted_findings_response([finding_json()]), tmp_root=tmp_path,
    )
    publisher = FakeReviewPublisher(
        pull_request=_pr_metadata(number=reviewed.pull_request_number, head_sha=reviewed.commit_sha),
        changed_files=reviewed.changed_files,
    )
    service = ReviewPublicationService(session_factory=session_factory, publisher=publisher)

    small_cap = await service.publish(
        review_run_id=reviewed.review_run_id, mode=ReviewPublicationMode.PUBLISH,
        config=PublicationConfig(enabled=True, min_severity=Severity.INFO, max_inline_comments=1),
    )
    large_cap = await service.publish(
        review_run_id=reviewed.review_run_id, mode=ReviewPublicationMode.PUBLISH,
        config=PublicationConfig(enabled=True, min_severity=Severity.INFO, max_inline_comments=5),
    )

    assert small_cap.status is ReviewPublicationStatus.PUBLISHED
    assert large_cap.status is ReviewPublicationStatus.PUBLISHED
    assert large_cap.reconciled is False
    assert large_cap.publication_id != small_cap.publication_id
    assert len(publisher.publish_calls) == 2

    rows = await _published_rows(session_factory, reviewed.review_run_id)
    published = [r for r in rows if r.status == ReviewPublicationStatus.PUBLISHED]
    assert len(published) == 2


async def test_comment_format_version_bump_produces_a_distinct_publication(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    """A PatchFrog-side formatting/engine change alone -- with a
    byte-for-byte identical repository config -- must still be treated as
    a materially different publication, never silently reused."""

    reviewed = await setup_reviewed_pull_request(
        session_factory, full_name="test/policy-drift-format-version", changed_lines=[14],
        response_factory=lambda req: scripted_findings_response([finding_json()]), tmp_root=tmp_path,
    )
    publisher = FakeReviewPublisher(
        pull_request=_pr_metadata(number=reviewed.pull_request_number, head_sha=reviewed.commit_sha),
        changed_files=reviewed.changed_files,
    )
    service = ReviewPublicationService(session_factory=session_factory, publisher=publisher)
    config = PublicationConfig(enabled=True, min_severity=Severity.INFO)

    before = await service.publish(review_run_id=reviewed.review_run_id, mode=ReviewPublicationMode.PUBLISH, config=config)

    with mock.patch.object(config_module, "COMMENT_FORMAT_VERSION", config_module.COMMENT_FORMAT_VERSION + 1):
        after = await service.publish(review_run_id=reviewed.review_run_id, mode=ReviewPublicationMode.PUBLISH, config=config)

    assert before.status is ReviewPublicationStatus.PUBLISHED
    assert after.status is ReviewPublicationStatus.PUBLISHED
    assert after.reconciled is False
    assert after.publication_id != before.publication_id
    assert len(publisher.publish_calls) == 2

    rows = await _published_rows(session_factory, reviewed.review_run_id)
    published = [r for r in rows if r.status == ReviewPublicationStatus.PUBLISHED]
    assert len(published) == 2
    assert len({r.publication_policy_fingerprint for r in published}) == 2
