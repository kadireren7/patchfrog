from __future__ import annotations

import json
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from patchfrog.persistence.models.review import ReviewCandidateModel, ReviewCandidateStatus
from patchfrog.review.domain import ReviewCandidate


class ReviewCandidateRepository:
    async def create(
        self, session: AsyncSession, *, review_run_id: uuid.UUID, candidate: ReviewCandidate
    ) -> ReviewCandidateModel:
        model = ReviewCandidateModel(
            review_run_id=review_run_id,
            file_path=candidate.file_path,
            symbol_id=candidate.symbol_id,
            symbol_name=candidate.symbol_name,
            qualified_name=candidate.qualified_name,
            start_line=candidate.start_line,
            end_line=candidate.end_line,
            changed_lines=json.dumps(list(candidate.changed_lines)),
            reason=candidate.reason,
            static_finding_ids=json.dumps([str(i) for i in candidate.static_finding_ids]),
            status=ReviewCandidateStatus.PENDING,
        )
        session.add(model)
        await session.flush()
        return model

    async def mark_reviewed(
        self, session: AsyncSession, *, candidate_id: uuid.UUID, context_bundle_id: uuid.UUID | None
    ) -> None:
        model = await session.get(ReviewCandidateModel, candidate_id)
        if model is None:
            raise ValueError(f"No review candidate with id {candidate_id}")
        model.status = ReviewCandidateStatus.REVIEWED
        model.context_bundle_id = context_bundle_id
        await session.flush()

    async def mark_failed(
        self, session: AsyncSession, *, candidate_id: uuid.UUID, error_message: str
    ) -> None:
        model = await session.get(ReviewCandidateModel, candidate_id)
        if model is None:
            raise ValueError(f"No review candidate with id {candidate_id}")
        model.status = ReviewCandidateStatus.FAILED
        model.error_message = error_message
        await session.flush()

    async def mark_skipped_budget(self, session: AsyncSession, *, candidate_id: uuid.UUID) -> None:
        model = await session.get(ReviewCandidateModel, candidate_id)
        if model is None:
            raise ValueError(f"No review candidate with id {candidate_id}")
        model.status = ReviewCandidateStatus.SKIPPED_BUDGET
        await session.flush()

    async def list_for_run(
        self, session: AsyncSession, *, review_run_id: uuid.UUID
    ) -> list[ReviewCandidateModel]:
        result = await session.execute(
            select(ReviewCandidateModel)
            .where(ReviewCandidateModel.review_run_id == review_run_id)
            .order_by(ReviewCandidateModel.file_path, ReviewCandidateModel.start_line)
        )
        return list(result.scalars().all())
