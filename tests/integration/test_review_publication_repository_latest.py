"""ReviewPublicationRepository.get_latest_for_review_run -- the one
query that answers "what actually happened to publication for this run"
without the caller needing to already know its `mode`/
`publication_policy_fingerprint` identity (unlike `get_published`/
`get_in_flight`). Added for a hosting operator (patchfrog-cloud) that
needs to distinguish "publish was never attempted" from "publish was
attempted and failed" when deriving its own job status -- see that
repo's own review-job lifecycle fix."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from patchfrog.persistence.models.publishing import ReviewPublicationModel
from patchfrog.persistence.repositories.review_publication import ReviewPublicationRepository
from patchfrog.publishing.config import PublicationConfig
from patchfrog.publishing.domain import ReviewPublicationMode, ReviewPublicationStatus
from tests.support.publishing import (
    finding_json,
    scripted_findings_response,
    setup_reviewed_pull_request,
)

_POLICY_FINGERPRINT = PublicationConfig(enabled=True).fingerprint()


async def test_no_publication_attempt_returns_none(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    reviewed = await setup_reviewed_pull_request(
        session_factory,
        full_name="test/no-publish-attempt",
        changed_lines=[10],
        response_factory=lambda req: scripted_findings_response([finding_json()]),
        tmp_root=tmp_path,
    )
    async with session_factory() as session:
        latest = await ReviewPublicationRepository().get_latest_for_review_run(
            session, review_run_id=reviewed.review_run_id
        )
    assert latest is None


async def test_returns_the_most_recent_attempt_regardless_of_mode_or_fingerprint(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    reviewed = await setup_reviewed_pull_request(
        session_factory,
        full_name="test/latest-publication",
        changed_lines=[10],
        response_factory=lambda req: scripted_findings_response([finding_json()]),
        tmp_root=tmp_path,
    )
    async with session_factory() as session:
        older = ReviewPublicationModel(
            review_run_id=reviewed.review_run_id,
            repository_id=reviewed.repository_id,
            pull_request_id=reviewed.pull_request_id,
            pull_request_number=reviewed.pull_request_number,
            head_sha=reviewed.commit_sha,
            mode=ReviewPublicationMode.DRY_RUN,
            publication_policy_fingerprint="a-different-fingerprint",
            status=ReviewPublicationStatus.FAILED,
            started_at=datetime.now(UTC) - timedelta(minutes=10),
            created_at=datetime.now(UTC) - timedelta(minutes=10),
        )
        newer = ReviewPublicationModel(
            review_run_id=reviewed.review_run_id,
            repository_id=reviewed.repository_id,
            pull_request_id=reviewed.pull_request_id,
            pull_request_number=reviewed.pull_request_number,
            head_sha=reviewed.commit_sha,
            mode=ReviewPublicationMode.PUBLISH,
            publication_policy_fingerprint=_POLICY_FINGERPRINT,
            status=ReviewPublicationStatus.PUBLISHED,
            started_at=datetime.now(UTC),
            created_at=datetime.now(UTC),
        )
        session.add_all([older, newer])
        await session.commit()

        latest = await ReviewPublicationRepository().get_latest_for_review_run(
            session, review_run_id=reviewed.review_run_id
        )
    assert latest is not None
    assert latest.id == newer.id
    assert latest.status is ReviewPublicationStatus.PUBLISHED


async def test_never_returns_a_different_runs_attempt(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    reviewed_a = await setup_reviewed_pull_request(
        session_factory,
        full_name="test/publication-scope-a",
        changed_lines=[10],
        response_factory=lambda req: scripted_findings_response([finding_json()]),
        tmp_root=tmp_path / "a",
    )
    reviewed_b = await setup_reviewed_pull_request(
        session_factory,
        full_name="test/publication-scope-b",
        changed_lines=[10],
        response_factory=lambda req: scripted_findings_response([finding_json()]),
        tmp_root=tmp_path / "b",
    )
    async with session_factory() as session:
        session.add(
            ReviewPublicationModel(
                review_run_id=reviewed_b.review_run_id,
                repository_id=reviewed_b.repository_id,
                pull_request_id=reviewed_b.pull_request_id,
                pull_request_number=reviewed_b.pull_request_number,
                head_sha=reviewed_b.commit_sha,
                mode=ReviewPublicationMode.PUBLISH,
                publication_policy_fingerprint=_POLICY_FINGERPRINT,
                status=ReviewPublicationStatus.PUBLISHED,
                started_at=datetime.now(UTC),
                created_at=datetime.now(UTC),
            )
        )
        await session.commit()

        latest_for_a = await ReviewPublicationRepository().get_latest_for_review_run(
            session, review_run_id=reviewed_a.review_run_id
        )
    assert latest_for_a is None
