"""Real-Postgres coverage for review_publications/review_publication_comments
schema guarantees: DB-level uniqueness (not just application logic) and
cascade lifecycle. Phase 6 spec section 45.

Mirrors ``tests/integration/test_review_context_bundle_cascade.py`` --
SQLite's test engine does not enforce foreign keys by default, so this
can only be verified for real against Postgres. Skips (rather than
failing) if a migrated Postgres isn't reachable at ``localhost:5432``.
"""

from __future__ import annotations

import shutil
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError, OperationalError, ProgrammingError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

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
from tests.support.publishing import (
    finding_json,
    scripted_findings_response,
    setup_reviewed_pull_request,
)

_POSTGRES_URL = "postgresql+asyncpg://patchfrog:patchfrog@localhost:5432/patchfrog"
_TEST_POLICY_FINGERPRINT = PublicationConfig(enabled=True).fingerprint()


async def _postgres_available() -> AsyncEngine | None:
    engine = create_async_engine(_POSTGRES_URL)
    try:
        async with engine.begin() as conn:
            await conn.execute(text("SELECT 1 FROM review_publications LIMIT 1"))
    except (OperationalError, ProgrammingError):
        await engine.dispose()
        return None
    return engine


async def _cleanup(session_factory: async_sessionmaker[AsyncSession], repository_id: uuid.UUID) -> None:
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


async def test_db_level_unique_constraint_rejects_second_published_row(tmp_path: Path) -> None:
    """Direct raw inserts, bypassing all application logic -- proves the
    protection is a real DB-level constraint, not just app-level
    discipline (spec section 19: "Use DB-level protection where
    appropriate")."""

    engine = await _postgres_available()
    if engine is None:
        pytest.skip("real PostgreSQL not reachable at localhost:5432 (docker compose up -d postgres)")

    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    repository_id = None
    try:
        reviewed = await setup_reviewed_pull_request(
            session_factory,
            full_name="db-constraint-test/publications",
            changed_lines=[14],
            response_factory=lambda req: scripted_findings_response([finding_json()]),
            tmp_root=tmp_path,
        )
        repository_id = reviewed.repository_id

        async with session_factory() as session:
            first = ReviewPublicationModel(
                review_run_id=reviewed.review_run_id, repository_id=repository_id,
                pull_request_id=reviewed.pull_request_id, pull_request_number=reviewed.pull_request_number,
                base_sha="0" * 40, head_sha=reviewed.commit_sha, mode=ReviewPublicationMode.PUBLISH, publication_policy_fingerprint=_TEST_POLICY_FINGERPRINT,
                status=ReviewPublicationStatus.PUBLISHED, github_review_id=1, started_at=datetime.now(UTC),
                completed_at=datetime.now(UTC),
            )
            session.add(first)
            await session.commit()

        async with session_factory() as session:
            second = ReviewPublicationModel(
                review_run_id=reviewed.review_run_id, repository_id=repository_id,
                pull_request_id=reviewed.pull_request_id, pull_request_number=reviewed.pull_request_number,
                base_sha="0" * 40, head_sha=reviewed.commit_sha, mode=ReviewPublicationMode.PUBLISH, publication_policy_fingerprint=_TEST_POLICY_FINGERPRINT,
                status=ReviewPublicationStatus.PUBLISHED, github_review_id=2, started_at=datetime.now(UTC),
                completed_at=datetime.now(UTC),
            )
            session.add(second)
            with pytest.raises(IntegrityError):
                await session.commit()
    finally:
        if repository_id is not None:
            await _cleanup(session_factory, repository_id)
        await engine.dispose()
        shutil.rmtree(tmp_path / "repo", ignore_errors=True)


async def test_dry_run_rows_never_block_a_later_published_row(tmp_path: Path) -> None:
    """DRY_RUN mode rows must never participate in the PUBLISH-mode
    uniqueness guarantee -- confirms the partial index correctly scopes
    on (review_run_id, mode) together, not review_run_id alone."""

    engine = await _postgres_available()
    if engine is None:
        pytest.skip("real PostgreSQL not reachable at localhost:5432 (docker compose up -d postgres)")

    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    repository_id = None
    try:
        reviewed = await setup_reviewed_pull_request(
            session_factory,
            full_name="db-constraint-test/dry-run-then-publish",
            changed_lines=[14],
            response_factory=lambda req: scripted_findings_response([finding_json()]),
            tmp_root=tmp_path,
        )
        repository_id = reviewed.repository_id

        async with session_factory() as session:
            dry_run_row = ReviewPublicationModel(
                review_run_id=reviewed.review_run_id, repository_id=repository_id,
                pull_request_id=reviewed.pull_request_id, pull_request_number=reviewed.pull_request_number,
                base_sha="0" * 40, head_sha=reviewed.commit_sha, mode=ReviewPublicationMode.DRY_RUN, publication_policy_fingerprint=_TEST_POLICY_FINGERPRINT,
                status=ReviewPublicationStatus.DRY_RUN, started_at=datetime.now(UTC), completed_at=datetime.now(UTC),
            )
            published_row = ReviewPublicationModel(
                review_run_id=reviewed.review_run_id, repository_id=repository_id,
                pull_request_id=reviewed.pull_request_id, pull_request_number=reviewed.pull_request_number,
                base_sha="0" * 40, head_sha=reviewed.commit_sha, mode=ReviewPublicationMode.PUBLISH,
                publication_policy_fingerprint=_TEST_POLICY_FINGERPRINT,
                status=ReviewPublicationStatus.PUBLISHED, github_review_id=1, started_at=datetime.now(UTC),
                completed_at=datetime.now(UTC),
            )
            session.add_all([dry_run_row, published_row])
            await session.commit()  # must succeed -- no conflict
    finally:
        if repository_id is not None:
            await _cleanup(session_factory, repository_id)
        await engine.dispose()
        shutil.rmtree(tmp_path / "repo", ignore_errors=True)


async def test_comment_fingerprint_uniqueness_within_a_publication(tmp_path: Path) -> None:
    engine = await _postgres_available()
    if engine is None:
        pytest.skip("real PostgreSQL not reachable at localhost:5432 (docker compose up -d postgres)")

    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    repository_id = None
    try:
        reviewed = await setup_reviewed_pull_request(
            session_factory,
            full_name="db-constraint-test/comment-fingerprint",
            changed_lines=[14],
            response_factory=lambda req: scripted_findings_response([finding_json()]),
            tmp_root=tmp_path,
        )
        repository_id = reviewed.repository_id
        finding_id = reviewed.findings[0].id

        async with session_factory() as session:
            publication = ReviewPublicationModel(
                review_run_id=reviewed.review_run_id, repository_id=repository_id,
                pull_request_id=reviewed.pull_request_id, pull_request_number=reviewed.pull_request_number,
                base_sha="0" * 40, head_sha=reviewed.commit_sha, mode=ReviewPublicationMode.DRY_RUN,
                publication_policy_fingerprint=_TEST_POLICY_FINGERPRINT,
                status=ReviewPublicationStatus.DRY_RUN, started_at=datetime.now(UTC),
            )
            session.add(publication)
            await session.commit()
            publication_id = publication.id

        async with session_factory() as session:
            comment = ReviewPublicationCommentModel(
                review_publication_id=publication_id, finding_id=finding_id, fingerprint="dup-fingerprint",
                path="src/billing.py", severity="medium", disposition=PublicationDisposition.INLINE,
                reason="mapped", side="new", line=14,
            )
            session.add(comment)
            await session.commit()

        async with session_factory() as session:
            duplicate = ReviewPublicationCommentModel(
                review_publication_id=publication_id, finding_id=finding_id, fingerprint="dup-fingerprint",
                path="src/billing.py", severity="medium", disposition=PublicationDisposition.INLINE,
                reason="mapped", side="new", line=14,
            )
            session.add(duplicate)
            with pytest.raises(IntegrityError):
                await session.commit()
    finally:
        if repository_id is not None:
            await _cleanup(session_factory, repository_id)
        await engine.dispose()
        shutil.rmtree(tmp_path / "repo", ignore_errors=True)


async def test_deleting_review_run_cascades_to_publications_and_comments(tmp_path: Path) -> None:
    engine = await _postgres_available()
    if engine is None:
        pytest.skip("real PostgreSQL not reachable at localhost:5432 (docker compose up -d postgres)")

    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    repository_id = None
    try:
        reviewed = await setup_reviewed_pull_request(
            session_factory,
            full_name="db-constraint-test/cascade",
            changed_lines=[14],
            response_factory=lambda req: scripted_findings_response([finding_json()]),
            tmp_root=tmp_path,
        )
        repository_id = reviewed.repository_id
        finding_id = reviewed.findings[0].id

        async with session_factory() as session:
            publication = ReviewPublicationModel(
                review_run_id=reviewed.review_run_id, repository_id=repository_id,
                pull_request_id=reviewed.pull_request_id, pull_request_number=reviewed.pull_request_number,
                base_sha="0" * 40, head_sha=reviewed.commit_sha, mode=ReviewPublicationMode.DRY_RUN,
                publication_policy_fingerprint=_TEST_POLICY_FINGERPRINT,
                status=ReviewPublicationStatus.DRY_RUN, started_at=datetime.now(UTC),
            )
            session.add(publication)
            await session.flush()
            comment = ReviewPublicationCommentModel(
                review_publication_id=publication.id, finding_id=finding_id, fingerprint="cascade-fp",
                path="src/billing.py", severity="medium", disposition=PublicationDisposition.INLINE,
                reason="mapped", side="new", line=14,
            )
            session.add(comment)
            await session.commit()
            publication_id = publication.id

        from patchfrog.persistence.models.review import ReviewRunModel

        async with session_factory() as session:
            run = await session.get(ReviewRunModel, reviewed.review_run_id)
            assert run is not None
            await session.delete(run)
            await session.commit()

        async with session_factory() as session:
            remaining_pub = await session.get(ReviewPublicationModel, publication_id)
            assert remaining_pub is None  # cascaded away with the review run
            remaining_comments = (
                await session.execute(
                    select(ReviewPublicationCommentModel).where(
                        ReviewPublicationCommentModel.review_publication_id == publication_id
                    )
                )
            ).scalars().all()
            assert remaining_comments == []  # cascaded transitively
    finally:
        if repository_id is not None:
            await _cleanup(session_factory, repository_id)
        await engine.dispose()
        shutil.rmtree(tmp_path / "repo", ignore_errors=True)
