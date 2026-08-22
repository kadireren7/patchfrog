from __future__ import annotations

import json
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from patchfrog.persistence.models.review import AIFindingProposalModel
from patchfrog.review.domain import AIReviewFinding, ProposalStatus


class AIFindingProposalRepository:
    """Persists every proposal for audit -- accepted or not. See the
    module docstring of :mod:`patchfrog.persistence.models.review`."""

    async def create(
        self,
        session: AsyncSession,
        *,
        review_run_id: uuid.UUID,
        candidate_id: uuid.UUID,
        finding: AIReviewFinding,
        status: ProposalStatus,
        validation_detail: str | None,
    ) -> AIFindingProposalModel:
        model = AIFindingProposalModel(
            review_run_id=review_run_id,
            candidate_id=candidate_id,
            title=finding.title,
            message=finding.message,
            category=finding.category,
            severity=finding.severity,
            confidence=finding.confidence,
            file_path=finding.file_path,
            start_line=finding.start_line,
            end_line=finding.end_line,
            evidence=json.dumps(
                [
                    {
                        "file_path": e.file_path,
                        "start_line": e.start_line,
                        "end_line": e.end_line,
                        "quoted_text": e.quoted_text,
                    }
                    for e in finding.evidence
                ]
            ),
            reasoning_summary=finding.reasoning_summary,
            suggested_fix=finding.suggested_fix,
            impact=finding.impact,
            status=status,
            validation_detail=validation_detail,
        )
        session.add(model)
        await session.flush()
        return model

    async def update_status(
        self,
        session: AsyncSession,
        *,
        proposal_id: uuid.UUID,
        status: ProposalStatus,
        validation_detail: str | None = None,
    ) -> None:
        model = await session.get(AIFindingProposalModel, proposal_id)
        if model is None:
            raise ValueError(f"No AI finding proposal with id {proposal_id}")
        model.status = status
        if validation_detail is not None:
            model.validation_detail = validation_detail
        await session.flush()

    async def list_for_run(
        self, session: AsyncSession, *, review_run_id: uuid.UUID
    ) -> list[AIFindingProposalModel]:
        result = await session.execute(
            select(AIFindingProposalModel).where(AIFindingProposalModel.review_run_id == review_run_id)
        )
        return list(result.scalars().all())
