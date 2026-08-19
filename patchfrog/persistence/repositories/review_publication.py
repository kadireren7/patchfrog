from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime, timedelta
from enum import StrEnum

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from patchfrog.persistence.models.publishing import ReviewPublicationModel
from patchfrog.publishing.domain import ReviewPublicationMode, ReviewPublicationStatus

#: How long a row may sit in ``PUBLISHING`` before a later attempt treats
#: it as abandoned (crashed) rather than a live concurrent publish. See
#: :meth:`ReviewPublicationRepository.get_or_create_attempt`.
IN_FLIGHT_GRACE = timedelta(minutes=5)


class PublicationAttemptOutcome(StrEnum):
    """What :meth:`ReviewPublicationRepository.get_or_create_attempt`
    decided about a new publication attempt for one identity."""

    NEW = "new"
    ALREADY_PUBLISHED = "already_published"
    IN_PROGRESS_ELSEWHERE = "in_progress_elsewhere"
    RECONCILE_NEEDED = "reconcile_needed"


class ReviewPublicationRepository:
    """Persistence operations for :class:`ReviewPublicationModel`.

    Identity for idempotency/concurrency purposes is ``(review_run_id,
    mode)`` -- mirrors :class:`patchfrog.persistence.repositories.review_run.ReviewRunRepository`
    exactly, including the transaction-scoped PostgreSQL advisory lock
    guarding creation/claim/success (no-op on SQLite). Only ``status =
    'published'`` rows participate in the uniqueness guarantee (see
    ``uq_review_publications_published_identity``) -- ``DRY_RUN`` attempts
    and failed/stale real-publish attempts never block a later successful
    publish for the same review run.
    """

    async def _lock_identity(
        self, session: AsyncSession, *, review_run_id: uuid.UUID, mode: ReviewPublicationMode
    ) -> None:
        if session.bind is None or session.bind.dialect.name != "postgresql":
            return
        key_material = f"review_publication:{review_run_id}:{mode.value}"
        digest = hashlib.sha256(key_material.encode()).digest()[:8]
        lock_key = int.from_bytes(digest, "big") & 0x7FFFFFFFFFFFFFFF
        await session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": lock_key})

    async def get_published(
        self, session: AsyncSession, *, review_run_id: uuid.UUID, mode: ReviewPublicationMode
    ) -> ReviewPublicationModel | None:
        result = await session.execute(
            select(ReviewPublicationModel).where(
                ReviewPublicationModel.review_run_id == review_run_id,
                ReviewPublicationModel.mode == mode,
                ReviewPublicationModel.status == ReviewPublicationStatus.PUBLISHED,
            )
        )
        return result.scalar_one_or_none()

    async def get_in_flight(
        self, session: AsyncSession, *, review_run_id: uuid.UUID, mode: ReviewPublicationMode
    ) -> ReviewPublicationModel | None:
        """A prior attempt stuck in ``PUBLISHING`` -- the durable marker
        for "GitHub write may have happened, DB commit did not" (see the
        module docstring of :mod:`patchfrog.publishing.service`)."""

        result = await session.execute(
            select(ReviewPublicationModel)
            .where(
                ReviewPublicationModel.review_run_id == review_run_id,
                ReviewPublicationModel.mode == mode,
                ReviewPublicationModel.status == ReviewPublicationStatus.PUBLISHING,
            )
            .order_by(ReviewPublicationModel.created_at.desc())
        )
        return result.scalars().first()

    async def create(
        self,
        session: AsyncSession,
        *,
        review_run_id: uuid.UUID,
        repository_id: uuid.UUID,
        pull_request_id: uuid.UUID | None,
        pull_request_number: int,
        base_sha: str | None,
        head_sha: str,
        mode: ReviewPublicationMode,
        status: ReviewPublicationStatus,
    ) -> ReviewPublicationModel:
        """Always creates a fresh row -- callers first check
        :meth:`get_published`/:meth:`get_in_flight` under the identity
        lock (see :mod:`patchfrog.publishing.service`) to decide whether a
        new attempt should even happen."""

        model = ReviewPublicationModel(
            review_run_id=review_run_id,
            repository_id=repository_id,
            pull_request_id=pull_request_id,
            pull_request_number=pull_request_number,
            base_sha=base_sha,
            head_sha=head_sha,
            mode=mode,
            status=status,
            started_at=datetime.now(UTC),
        )
        session.add(model)
        await session.flush()
        return model

    async def get_or_create_attempt(
        self,
        session: AsyncSession,
        *,
        review_run_id: uuid.UUID,
        repository_id: uuid.UUID,
        pull_request_id: uuid.UUID | None,
        pull_request_number: int,
        base_sha: str | None,
        head_sha: str,
        mode: ReviewPublicationMode,
        initial_status: ReviewPublicationStatus,
    ) -> tuple[ReviewPublicationModel, PublicationAttemptOutcome]:
        """Single atomic decision point for one publication attempt,
        mirroring :meth:`patchfrog.persistence.repositories.review_run.ReviewRunRepository.get_or_create_running`
        exactly: lock the identity, then decide.

        - A prior ``PUBLISHED`` row for this identity -> ``ALREADY_PUBLISHED``,
          no write needed (see :mod:`patchfrog.publishing.service`).
        - A prior ``PUBLISHING`` row still within :data:`IN_FLIGHT_GRACE` of
          its ``started_at`` -> ``IN_PROGRESS_ELSEWHERE``: another attempt
          (this process or a concurrent worker) may be mid-GitHub-call
          right now; this caller must not write.
        - A prior ``PUBLISHING`` row *past* :data:`IN_FLIGHT_GRACE` ->
          ``RECONCILE_NEEDED``: likely abandoned (crashed) -- the caller
          should check GitHub for a matching marker before deciding
          whether to supersede it (see
          :meth:`patchfrog.publishing.github_publisher.GitHubClientReviewPublisher.find_patchfrog_review`).
        - Otherwise -> ``NEW``: a freshly created row, safe to proceed.

        For ``PUBLISH`` mode the fresh row is created directly with
        ``initial_status`` already reflecting the caller's intent (e.g.
        ``PUBLISHING``) so the *entire* check-then-create decision is one
        atomic, lock-guarded operation -- no window between "no attempt
        exists" and "an attempt now exists" that a concurrent second
        caller could slip through.
        """

        await self._lock_identity(session, review_run_id=review_run_id, mode=mode)

        published = await self.get_published(session, review_run_id=review_run_id, mode=mode)
        if published is not None:
            return published, PublicationAttemptOutcome.ALREADY_PUBLISHED

        in_flight = await self.get_in_flight(session, review_run_id=review_run_id, mode=mode)
        if in_flight is not None:
            # SQLite (used in the test suite) does not round-trip tzinfo
            # through a DateTime(timezone=True) column -- a naive value
            # read back is always the UTC we wrote, so it's safe to
            # re-attach it rather than fail the subtraction below. Real
            # Postgres always returns a tz-aware value already.
            started_at = in_flight.started_at
            if started_at.tzinfo is None:
                started_at = started_at.replace(tzinfo=UTC)
            age = datetime.now(UTC) - started_at
            if age < IN_FLIGHT_GRACE:
                return in_flight, PublicationAttemptOutcome.IN_PROGRESS_ELSEWHERE
            return in_flight, PublicationAttemptOutcome.RECONCILE_NEEDED

        model = await self.create(
            session,
            review_run_id=review_run_id,
            repository_id=repository_id,
            pull_request_id=pull_request_id,
            pull_request_number=pull_request_number,
            base_sha=base_sha,
            head_sha=head_sha,
            mode=mode,
            status=initial_status,
        )
        return model, PublicationAttemptOutcome.NEW

    async def supersede_abandoned(
        self, session: AsyncSession, *, publication_id: uuid.UUID
    ) -> ReviewPublicationModel:
        """Mark an abandoned (past-grace, no GitHub marker found)
        ``PUBLISHING`` row ``FAILED`` so a fresh attempt can take over its
        identity. Must be called under the same identity lock that
        produced ``RECONCILE_NEEDED`` (see :meth:`get_or_create_attempt`)."""

        return await self.mark_failed(
            session, publication_id=publication_id, error_message="abandoned in-flight publication attempt (past grace period); superseded by retry"
        )

    async def mark_publishing(self, session: AsyncSession, *, publication_id: uuid.UUID) -> ReviewPublicationModel:
        """Durably record intent *before* the GitHub write is attempted --
        the crash-recovery marker (see the module docstring)."""

        model = await session.get(ReviewPublicationModel, publication_id)
        if model is None:
            raise ValueError(f"No review publication with id {publication_id}")
        model.status = ReviewPublicationStatus.PUBLISHING
        await session.flush()
        return model

    async def mark_published(
        self,
        session: AsyncSession,
        *,
        publication_id: uuid.UUID,
        github_review_id: int,
        inline_count: int,
        summary_only_count: int,
        omitted_count: int,
        reconciled: bool = False,
    ) -> ReviewPublicationModel:
        """Mark a publication attempt published. Returns the *canonical*
        row for this identity -- mirrors
        :meth:`patchfrog.persistence.repositories.review_run.ReviewRunRepository.mark_succeeded`
        exactly: re-acquires the identity lock and re-checks for a
        concurrent winner before writing, because the lock acquired by
        :meth:`get_or_create_attempt` is released well before this call
        (it must not be held across the external GitHub write -- see the
        module docstring of :mod:`patchfrog.publishing.service`). Without
        this re-check, two attempts that both reached ``PUBLISHING``
        (impossible for the *same* transaction window, but not across the
        gap between the two locked sections) could each try to become the
        canonical ``PUBLISHED`` row and collide against
        ``uq_review_publications_published_identity`` -- or worse, only
        one of the two lock windows actually contends, since a GitHub
        write genuinely takes real wall-clock time between them.
        """

        model = await session.get(ReviewPublicationModel, publication_id)
        if model is None:
            raise ValueError(f"No review publication with id {publication_id}")

        await self._lock_identity(session, review_run_id=model.review_run_id, mode=model.mode)
        existing = await self.get_published(session, review_run_id=model.review_run_id, mode=model.mode)
        if existing is not None and existing.id != model.id:
            model.status = ReviewPublicationStatus.FAILED
            model.error_message = f"superseded by concurrent publication {existing.id}"
            model.completed_at = datetime.now(UTC)
            await session.flush()
            return existing

        model.status = ReviewPublicationStatus.PUBLISHED
        model.github_review_id = github_review_id
        model.inline_count = inline_count
        model.summary_only_count = summary_only_count
        model.omitted_count = omitted_count
        model.reconciled = reconciled
        model.completed_at = datetime.now(UTC)
        await session.flush()
        return model

    async def mark_failed(
        self, session: AsyncSession, *, publication_id: uuid.UUID, error_message: str
    ) -> ReviewPublicationModel:
        model = await session.get(ReviewPublicationModel, publication_id)
        if model is None:
            raise ValueError(f"No review publication with id {publication_id}")
        model.status = ReviewPublicationStatus.FAILED
        model.error_message = error_message
        model.completed_at = datetime.now(UTC)
        await session.flush()
        return model

    async def get_by_id(
        self, session: AsyncSession, *, publication_id: uuid.UUID
    ) -> ReviewPublicationModel | None:
        return await session.get(ReviewPublicationModel, publication_id)
