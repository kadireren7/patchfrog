"""Read-only conversions from persisted Phase 5 output into the
publication planner's plain input types.

Keeps :mod:`patchfrog.publishing.planner` free of any SQLAlchemy/ORM
dependency -- see that module's docstring on unit-testability.
"""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from patchfrog.persistence.models.review import AIFindingModel
from patchfrog.publishing.domain import PublishableFinding
from patchfrog.review.queries import ReviewQueryService


def publishable_finding_from_model(model: AIFindingModel) -> PublishableFinding:
    return PublishableFinding(
        finding_id=model.id,
        title=model.title,
        message=model.message,
        category=model.category,
        severity=model.severity,
        confidence=model.confidence,
        file_path=model.file_path,
        start_line=model.start_line,
        end_line=model.end_line,
        reasoning_summary=model.reasoning_summary,
        suggested_fix=model.suggested_fix,
    )


async def get_publishable_findings(
    session: AsyncSession, *, review_run_id: uuid.UUID, query_service: ReviewQueryService | None = None
) -> list[PublishableFinding]:
    """The exact set of findings a publication plan should consider for
    ``review_run_id`` -- the same ``ai_findings`` rows
    :class:`~patchfrog.review.queries.ReviewQueryService.get_findings_for_run`
    already exposes as the one user-facing query surface."""

    service = query_service or ReviewQueryService()
    models = await service.get_findings_for_run(session, review_run_id=review_run_id)
    return [publishable_finding_from_model(m) for m in models]
