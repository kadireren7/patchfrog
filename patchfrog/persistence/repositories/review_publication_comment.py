from __future__ import annotations

import hashlib
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from patchfrog.persistence.models.publishing import (
    ReviewPublicationCommentModel,
    ReviewPublicationModel,
)
from patchfrog.publishing.domain import (
    PublicationDisposition,
    ReviewPublicationComment,
    ReviewPublicationStatus,
)


def _body_hash(body: str) -> str | None:
    if not body:
        return None
    return hashlib.sha256(body.encode()).hexdigest()


class ReviewPublicationCommentRepository:
    async def create(
        self, session: AsyncSession, *, review_publication_id: uuid.UUID, comment: ReviewPublicationComment
    ) -> ReviewPublicationCommentModel:
        position = comment.position
        model = ReviewPublicationCommentModel(
            review_publication_id=review_publication_id,
            finding_id=comment.finding_id,
            fingerprint=comment.fingerprint,
            path=comment.path,
            severity=comment.severity,
            disposition=comment.disposition,
            reason=comment.reason,
            side=position.side.value if position is not None else None,
            line=position.line if position is not None else None,
            start_side=(position.start_side.value if position and position.start_side is not None else None),
            start_line=position.start_line if position is not None else None,
            body_hash=_body_hash(comment.body),
        )
        session.add(model)
        await session.flush()
        return model

    async def set_github_comment_id(
        self, session: AsyncSession, *, comment_id: uuid.UUID, github_comment_id: int
    ) -> None:
        model = await session.get(ReviewPublicationCommentModel, comment_id)
        if model is None:
            raise ValueError(f"No review publication comment with id {comment_id}")
        model.github_comment_id = github_comment_id
        await session.flush()

    async def list_for_publication(
        self, session: AsyncSession, *, review_publication_id: uuid.UUID
    ) -> list[ReviewPublicationCommentModel]:
        result = await session.execute(
            select(ReviewPublicationCommentModel)
            .where(ReviewPublicationCommentModel.review_publication_id == review_publication_id)
            .order_by(ReviewPublicationCommentModel.path, ReviewPublicationCommentModel.line)
        )
        return list(result.scalars().all())

    async def list_actually_published_finding_ids(
        self, session: AsyncSession, *, finding_ids: frozenset[uuid.UUID]
    ) -> frozenset[uuid.UUID]:
        """Which of ``finding_ids`` were part of a publication that
        actually reached GitHub -- i.e. a comment row (``INLINE`` or
        ``SUMMARY_ONLY`` disposition; ``ALREADY_REPORTED``/``OMITTED``
        never wrote anything) attached to a publication whose *parent*
        row's status is ``PUBLISHED``. Comment rows are persisted for
        every attempt regardless of outcome (dry-run, stale, disabled,
        failed), so the parent's status is the only reliable signal that
        a real GitHub write happened -- see the ``_persist_plan_comments``
        call site in :mod:`patchfrog.publishing.service`, which runs
        before any of those outcomes are known.

        Used by :func:`patchfrog.publishing.queries.get_current_active_findings`
        to tell a genuinely never-published carried-forward finding (safe
        to publish now that a gate has opened) apart from one that was
        already reported to the PR in an earlier review's real write
        (must never be re-published)."""

        if not finding_ids:
            return frozenset()
        result = await session.execute(
            select(ReviewPublicationCommentModel.finding_id)
            .join(
                ReviewPublicationModel,
                ReviewPublicationCommentModel.review_publication_id == ReviewPublicationModel.id,
            )
            .where(
                ReviewPublicationCommentModel.finding_id.in_(finding_ids),
                ReviewPublicationModel.status == ReviewPublicationStatus.PUBLISHED,
                ReviewPublicationCommentModel.disposition.in_(
                    (PublicationDisposition.INLINE, PublicationDisposition.SUMMARY_ONLY)
                ),
            )
        )
        return frozenset(fid for fid in result.scalars().all() if fid is not None)
