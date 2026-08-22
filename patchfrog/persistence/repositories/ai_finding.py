from __future__ import annotations

import json
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from patchfrog.persistence.models.review import AIFindingModel
from patchfrog.review.domain import FinalAIFinding


class AIFindingRepository:
    """The only table a presentation/query layer should read from -- see
    the module docstring of :mod:`patchfrog.persistence.models.review`."""

    async def bulk_create(
        self, session: AsyncSession, *, review_run_id: uuid.UUID, findings: list[FinalAIFinding]
    ) -> list[AIFindingModel]:
        models = []
        for f in findings:
            model = AIFindingModel(
                review_run_id=review_run_id,
                proposal_id=f.proposal_id,
                candidate_id=f.candidate_id,
                title=f.finding.title,
                message=f.finding.message,
                category=f.finding.category,
                severity=f.final_severity,
                confidence=f.final_confidence,
                file_path=f.finding.file_path,
                start_line=f.finding.start_line,
                end_line=f.finding.end_line,
                evidence=json.dumps(
                    [
                        {
                            "file_path": e.file_path,
                            "start_line": e.start_line,
                            "end_line": e.end_line,
                            "quoted_text": e.quoted_text,
                        }
                        for e in f.finding.evidence
                    ]
                ),
                reasoning_summary=f.finding.reasoning_summary,
                suggested_fix=f.finding.suggested_fix,
                impact=f.finding.impact,
                corroborated_by_static=f.corroborated_by_static,
                static_finding_ids=json.dumps([str(i) for i in f.static_finding_ids]),
            )
            models.append(model)
        session.add_all(models)
        await session.flush()
        return models

    async def list_for_run(self, session: AsyncSession, *, review_run_id: uuid.UUID) -> list[AIFindingModel]:
        result = await session.execute(
            select(AIFindingModel)
            .where(AIFindingModel.review_run_id == review_run_id)
            .order_by(AIFindingModel.file_path, AIFindingModel.start_line)
        )
        return list(result.scalars().all())
