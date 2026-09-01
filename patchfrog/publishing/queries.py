"""Read-only conversions from persisted Phase 5 output into the
publication planner's plain input types.

Keeps :mod:`patchfrog.publishing.planner` free of any SQLAlchemy/ORM
dependency -- see that module's docstring on unit-testability.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from patchfrog.analysis.security_rule_metadata import lookup as lookup_security_rule_metadata
from patchfrog.persistence.models.analysis import FindingModel
from patchfrog.persistence.models.review import AIFindingModel
from patchfrog.persistence.repositories.review_memory_finding import ReviewMemoryFindingRepository
from patchfrog.persistence.repositories.review_publication_comment import (
    ReviewPublicationCommentRepository,
)
from patchfrog.publishing.domain import PublishableFinding
from patchfrog.review.queries import ReviewQueryService


def publishable_finding_from_static_finding(model: FindingModel) -> PublishableFinding:
    """Projects a Phase 3 static finding into the same
    :class:`PublishableFinding` shape an AI finding produces, so
    :func:`patchfrog.publishing.body.format_inline_comment_body` can
    render either origin through one pipeline (Phase 8 spec section 8/28)
    -- never AI-authored reasoning bolted onto a deterministic rule.

    Not wired into :mod:`patchfrog.publishing.planner`/``service`` --
    static findings still have no production publication *trigger* in
    this refinement (a materially larger change: dedup against AI
    findings, diff-position mapping, a publication-run identity that
    currently only knows about ``review_runs``). This function proves
    the formatter itself is origin-agnostic; wiring a real "publish
    static findings" flow is out of scope here.
    """

    metadata = lookup_security_rule_metadata(model.rule_id)
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
        reasoning_summary=metadata.reason if metadata is not None else "",
        suggested_fix=metadata.remediation if metadata is not None else None,
        impact=None,  # never fabricated from a static rule alone -- see the module docstring
    )


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
        impact=model.impact,
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


async def get_current_active_findings(
    session: AsyncSession, *, review_run_id: uuid.UUID, query_service: ReviewQueryService | None = None
) -> tuple[list[PublishableFinding], frozenset[uuid.UUID]]:
    """The canonical publishable-finding set for one review run: this
    run's own fresh ``ai_findings`` rows (:func:`get_publishable_findings`),
    plus any Phase 7 (:mod:`patchfrog.review_memory`) finding that was
    zero-AI-call carried forward *to this exact run* -- symbol continuity
    UNCHANGED and its stored evidence independently reconfirmed verbatim
    at this run's own commit (see
    :meth:`patchfrog.review_memory.service.IncrementalReviewMemoryService.finalize`)
    -- and has never actually been published to GitHub before.

    Closes a gap in the original Phase 6/7 integration: a carried-forward
    finding was always *advisory-visible* (queryable via
    :class:`~patchfrog.persistence.repositories.review_memory_finding.ReviewMemoryFindingRepository`)
    but never entered a run's own publishable set, because that set was
    only ever this run's own fresh ``ai_findings`` rows. A finding that
    was accepted once, never published (e.g. publishing was disabled at
    the time), and then correctly carried forward with zero further
    provider calls therefore had no way to ever become publishable --
    see ``validation/production_e2e/latest-summary.md``'s "known
    limitation" section for the real dogfood run that exposed this.

    Second return value is ``already_reported_finding_ids`` for
    :meth:`patchfrog.publishing.planner.PublicationPlanner.build_plan`:
    a carried-forward finding that genuinely already reached GitHub in an
    earlier publication is deliberately *not* added to the findings list
    at all here (never re-planned), and is reported back so callers can
    still record its ``ALREADY_REPORTED`` disposition in this run's own
    plan (see
    :meth:`patchfrog.persistence.repositories.review_publication_comment.ReviewPublicationCommentRepository.list_actually_published_finding_ids`
    for exactly what "genuinely already reached GitHub" means).

    Every decision here is a plain re-read of state
    :mod:`patchfrog.review_memory` and :mod:`patchfrog.publishing`
    already persisted -- no continuity/evidence classification is
    redone, no LLM is called, and a finding whose memory status is
    anything other than ``CARRIED_FORWARD`` for *this exact*
    ``review_run_id`` (e.g. ``CHANGED``, ``RESOLVED``, ``AMBIGUOUS``, or
    simply not yet reconciled against this run) is never considered --
    see :meth:`ReviewMemoryFindingRepository.list_carried_forward_current_finding_ids`.
    """

    service = query_service or ReviewQueryService()
    fresh = await get_publishable_findings(session, review_run_id=review_run_id, query_service=service)
    fresh_ids = {f.finding_id for f in fresh}

    carried_ids = await ReviewMemoryFindingRepository().list_carried_forward_current_finding_ids(
        session, review_run_id=review_run_id
    )
    candidate_ids = frozenset(carried_ids) - fresh_ids
    if not candidate_ids:
        return fresh, frozenset()

    already_published = await ReviewPublicationCommentRepository().list_actually_published_finding_ids(
        session, finding_ids=candidate_ids
    )
    newly_addable = candidate_ids - already_published
    if newly_addable:
        result = await session.execute(select(AIFindingModel).where(AIFindingModel.id.in_(newly_addable)))
        for model in result.scalars().all():
            fresh.append(publishable_finding_from_model(model))

    return fresh, already_published
