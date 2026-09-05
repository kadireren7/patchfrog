"""The one bounded, indexed SQL query this milestone needs -- fetches
trusted historical findings for a repository, as of a specific point in
time, directly from Phase 9's own already-persisted ``feedback_events``
table joined with the existing
``ai_findings``/``review_candidates``/``review_runs`` chain.

**No new table, no new history database.** See
``validation/historical_regression_memory/latest-summary.md`` section 1
("How does this reuse Review Memory") for the full audit. One query per
review run -- never a per-current-surface query loop (spec section 13:
"Avoid per-event DB query loops", mirrored from Phase 9's own
:mod:`patchfrog.feedback.queries` discipline).

**Point-in-time, not "current state" (spec sections 5/6/9)**: trust is
computed from ``feedback_events`` rows with ``occurred_at <= as_of``
only -- never from the persisted ``feedback_assessments`` snapshot
(which reflects trust *now*, not trust as of the current review's own
temporal boundary). A finding created before ``as_of`` but only marked
``fixed``/``useful`` *after* ``as_of`` is correctly invisible; a
``false-positive``/``ignore`` recorded *after* ``as_of`` correctly does
not (yet) exclude it. ``as_of`` is always the current review run's own
persisted ``started_at`` (see :mod:`patchfrog.review.service`'s
integration point) -- reproducible for a given review run, never a
fresh wall-clock read that would differ across retries.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import ColumnElement, case, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from patchfrog.feedback.domain import ExplicitCommand, FeedbackEventType
from patchfrog.historical_regression_memory.domain import (
    MAX_EVIDENCE_FINGERPRINT_CHARS,
    MAX_HISTORICAL_LOOKBACK_ROWS,
    HistoricalEvidenceStrength,
    HistoricalRegressionRecord,
)
from patchfrog.persistence.models.feedback import FeedbackEventModel
from patchfrog.persistence.models.review import AIFindingModel, ReviewCandidateModel, ReviewRunModel


async def fetch_trusted_historical_records(
    session: AsyncSession,
    *,
    repository_id: uuid.UUID,
    as_of: datetime,
    limit: int = MAX_HISTORICAL_LOOKBACK_ROWS,
) -> tuple[HistoricalRegressionRecord, ...]:
    """The mandatory eligibility predicate (spec sections 2/11, audited
    in section 2 of the validation doc): ``(fixed_count > 0 OR
    useful_count > 0) AND false_positive_count = 0 AND ignore_count = 0``
    -- computed from ``EXPLICIT_COMMAND`` events with ``occurred_at <=
    as_of`` only, scoped by a mandatory ``repository_id`` equality
    filter on both the raw events and the joined review run (never
    optional -- see the audit's "Repository isolation" section).
    Ordered strongest-trust-first (a confirmed fix outranks "useful"
    alone), then most-recently-trusted, then a stable id tie-break --
    never returns more than ``limit`` rows."""

    def _count(command: ExplicitCommand) -> ColumnElement[int]:
        return case((FeedbackEventModel.normalized_signal == command.value, 1), else_=0)

    fixed_count = _count(ExplicitCommand.FIXED)
    useful_count = _count(ExplicitCommand.USEFUL)
    false_positive_count = _count(ExplicitCommand.FALSE_POSITIVE)
    ignore_count = _count(ExplicitCommand.IGNORE)
    trusted_at_case = case(
        (
            FeedbackEventModel.normalized_signal.in_((ExplicitCommand.FIXED.value, ExplicitCommand.USEFUL.value)),
            FeedbackEventModel.occurred_at,
        )
    )

    trust = (
        select(
            FeedbackEventModel.finding_id.label("finding_id"),
            func.sum(fixed_count).label("fixed_count"),
            func.sum(useful_count).label("useful_count"),
            func.sum(false_positive_count).label("false_positive_count"),
            func.sum(ignore_count).label("ignore_count"),
            func.min(trusted_at_case).label("trusted_at"),
        )
        .where(
            FeedbackEventModel.repository_id == repository_id,
            FeedbackEventModel.event_type == FeedbackEventType.EXPLICIT_COMMAND,
            FeedbackEventModel.occurred_at <= as_of,
            FeedbackEventModel.finding_id.is_not(None),
        )
        .group_by(FeedbackEventModel.finding_id)
        .having(
            or_(func.sum(fixed_count) > 0, func.sum(useful_count) > 0),
            func.sum(false_positive_count) == 0,
            func.sum(ignore_count) == 0,
        )
        .subquery()
    )

    trust_rank = case((trust.c.fixed_count > 0, 0), else_=1)

    stmt = (
        select(
            AIFindingModel.id,
            AIFindingModel.file_path,
            AIFindingModel.category,
            AIFindingModel.title,
            ReviewCandidateModel.qualified_name,
            ReviewRunModel.id.label("review_run_id"),
            ReviewRunModel.commit_sha,
            trust.c.fixed_count,
            trust.c.trusted_at,
        )
        .select_from(trust)
        .join(AIFindingModel, AIFindingModel.id == trust.c.finding_id)
        .join(ReviewCandidateModel, ReviewCandidateModel.id == AIFindingModel.candidate_id)
        .join(ReviewRunModel, ReviewRunModel.id == AIFindingModel.review_run_id)
        .where(ReviewRunModel.repository_id == repository_id)
        .order_by(trust_rank, trust.c.trusted_at.desc(), AIFindingModel.id)
        .limit(limit)
    )

    rows = (await session.execute(stmt)).all()

    records: list[HistoricalRegressionRecord] = []
    for row in rows:
        strength = (
            HistoricalEvidenceStrength.CONFIRMED_FIXED
            if row.fixed_count > 0
            else HistoricalEvidenceStrength.CONFIRMED_USEFUL
        )
        title = (row.title or "").strip()
        fingerprint = title[:MAX_EVIDENCE_FINGERPRINT_CHARS]
        records.append(
            HistoricalRegressionRecord(
                historical_finding_id=row.id,
                repository_id=repository_id,
                historical_review_run_id=row.review_run_id,
                historical_commit_sha=row.commit_sha,
                source_file_path=row.file_path,
                source_qualified_name=row.qualified_name,
                finding_category=row.category,
                evidence_strength=strength,
                bounded_evidence_fingerprint=fingerprint,
                observed_at=row.trusted_at.isoformat(),
            )
        )
    return tuple(records)
