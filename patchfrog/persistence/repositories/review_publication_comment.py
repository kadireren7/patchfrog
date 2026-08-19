from __future__ import annotations

import hashlib
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from patchfrog.persistence.models.publishing import ReviewPublicationCommentModel
from patchfrog.publishing.domain import ReviewPublicationComment


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
