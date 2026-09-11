"""The one new bounded, indexed query this milestone needs: repeated,
uncontradicted false-positive feedback on one exact structural surface
(Y5). Mirrors :func:`patchfrog.historical_regression_memory.queries.fetch_trusted_historical_records`'s
own shape and trust discipline exactly (same tables, same
point-in-time ``as_of`` semantics, same mandatory ``repository_id``
scoping) -- inverted: that query requires
``false_positive_count == 0``; this one requires it to be the *only*
signal (``false_positive_count > 0 AND fixed_count == 0 AND
useful_count == 0``), so a surface that later received a contradicting
useful/fixed signal is correctly excluded (see
:mod:`patchfrog.learning_records.service`'s own retirement handling for
what happens to an *existing* persisted record in that case).

No new table, no new history database -- reads directly from the
existing ``feedback_events``/``ai_findings``/``review_candidates``/
``review_runs`` chain.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import ColumnElement, case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from patchfrog.analysis.domain import FindingCategory
from patchfrog.feedback.domain import ExplicitCommand, FeedbackEventType
from patchfrog.persistence.models.feedback import FeedbackEventModel
from patchfrog.persistence.models.review import AIFindingModel, ReviewCandidateModel, ReviewRunModel

#: Same defensive ceiling rationale as
#: patchfrog.historical_regression_memory.domain.MAX_HISTORICAL_LOOKBACK_ROWS.
MAX_NOISE_LOOKBACK_ROWS = 500


@dataclass(frozen=True, slots=True)
class NoiseFeedbackRow:
    """One finding with pure, uncontradicted false-positive feedback --
    not yet grouped by surface (see
    :func:`patchfrog.learning_records.service.recompute_repository_learnings`
    for the grouping/independence step, mirroring Milestone O's own
    grouping discipline exactly)."""

    finding_id: uuid.UUID
    file_path: str
    qualified_name: str | None
    category: FindingCategory
    review_run_id: uuid.UUID
    observed_at: datetime


async def fetch_repeated_noise_feedback(
    session: AsyncSession, *, repository_id: uuid.UUID, as_of: datetime, limit: int = MAX_NOISE_LOOKBACK_ROWS
) -> tuple[NoiseFeedbackRow, ...]:
    """Every finding in ``repository_id`` with only ``false-positive``
    explicit-command feedback (``occurred_at <= as_of``, never a
    future-leaking read) -- never a finding that also has any
    ``fixed``/``useful`` signal, however old. Grouping into independent,
    per-surface support counts happens in the caller, exactly like
    Milestone O's own grouping happens outside Milestone N's query."""

    def _count(command: ExplicitCommand) -> ColumnElement[int]:
        return case((FeedbackEventModel.normalized_signal == command.value, 1), else_=0)

    false_positive_count = _count(ExplicitCommand.FALSE_POSITIVE)
    fixed_count = _count(ExplicitCommand.FIXED)
    useful_count = _count(ExplicitCommand.USEFUL)

    trust = (
        select(
            FeedbackEventModel.finding_id.label("finding_id"),
            func.sum(false_positive_count).label("false_positive_count"),
            func.max(FeedbackEventModel.occurred_at).label("observed_at"),
        )
        .where(
            FeedbackEventModel.repository_id == repository_id,
            FeedbackEventModel.event_type == FeedbackEventType.EXPLICIT_COMMAND,
            FeedbackEventModel.occurred_at <= as_of,
            FeedbackEventModel.finding_id.is_not(None),
        )
        .group_by(FeedbackEventModel.finding_id)
        .having(
            func.sum(false_positive_count) > 0,
            func.sum(fixed_count) == 0,
            func.sum(useful_count) == 0,
        )
        .subquery()
    )

    stmt = (
        select(
            AIFindingModel.id,
            AIFindingModel.file_path,
            AIFindingModel.category,
            ReviewCandidateModel.qualified_name,
            ReviewRunModel.id.label("review_run_id"),
            trust.c.observed_at,
        )
        .select_from(trust)
        .join(AIFindingModel, AIFindingModel.id == trust.c.finding_id)
        .join(ReviewCandidateModel, ReviewCandidateModel.id == AIFindingModel.candidate_id)
        .join(ReviewRunModel, ReviewRunModel.id == AIFindingModel.review_run_id)
        .where(ReviewRunModel.repository_id == repository_id)
        .order_by(trust.c.observed_at.desc(), AIFindingModel.id)
        .limit(limit)
    )

    rows = (await session.execute(stmt)).all()
    return tuple(
        NoiseFeedbackRow(
            finding_id=row.id,
            file_path=row.file_path,
            qualified_name=row.qualified_name,
            category=row.category,
            review_run_id=row.review_run_id,
            observed_at=row.observed_at,
        )
        for row in rows
    )
