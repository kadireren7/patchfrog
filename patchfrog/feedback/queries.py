"""Read-only query layer for Phase 9 feedback -- the surface the CLI and
any future UI/analytics code should use.

Every summary here is computed live from raw events via
:func:`patchfrog.feedback.assessment.compute_finding_assessment` --
always consistent with the latest raw data, never stale.
:func:`recompute_and_persist_all` additionally writes this into
``feedback_assessments`` for callers that want a queryable, versioned
snapshot without recomputing on every read (Phase 9 spec section 36).

Every multi-finding query here issues exactly one raw-event query, then
groups in Python -- never one query per finding (Phase 9 spec section 51:
"Avoid per-event DB query loops.").
"""

from __future__ import annotations

import uuid
from collections import defaultdict

from sqlalchemy.ext.asyncio import AsyncSession

from patchfrog.feedback.assessment import (
    compute_finding_assessment,
    is_false_positive_candidate,
    is_high_value_candidate,
)
from patchfrog.feedback.domain import (
    FEEDBACK_ASSESSMENT_VERSION,
    FeedbackEvent,
    FindingFeedbackSummary,
)
from patchfrog.persistence.repositories.feedback import (
    FeedbackAssessmentRepository,
    FeedbackEventRepository,
    feedback_event_from_model,
)


async def get_feedback_for_finding(session: AsyncSession, *, finding_id: uuid.UUID) -> list[FeedbackEvent]:
    models = await FeedbackEventRepository().list_for_finding(session, finding_id=finding_id)
    return [feedback_event_from_model(m) for m in models]


async def get_feedback_summary_for_finding(
    session: AsyncSession, *, finding_id: uuid.UUID
) -> FindingFeedbackSummary:
    events = await get_feedback_for_finding(session, finding_id=finding_id)
    return compute_finding_assessment(finding_id, events)


async def get_feedback_for_pr(session: AsyncSession, *, pull_request_id: uuid.UUID) -> list[FeedbackEvent]:
    models = await FeedbackEventRepository().list_for_pull_request(session, pull_request_id=pull_request_id)
    return [feedback_event_from_model(m) for m in models]


async def get_feedback_for_review(session: AsyncSession, *, review_run_id: uuid.UUID) -> list[FeedbackEvent]:
    models = await FeedbackEventRepository().list_for_review_run(session, review_run_id=review_run_id)
    return [feedback_event_from_model(m) for m in models]


async def get_unresolved_feedback(
    session: AsyncSession, *, repository_id: uuid.UUID | None = None
) -> list[FeedbackEvent]:
    """Events that could not be attributed to an exact finding -- see
    :mod:`patchfrog.feedback.attribution`. Still recorded, never
    discarded; just excluded from any per-finding summary."""

    models = await FeedbackEventRepository().list_unattributed(session, repository_id=repository_id)
    return [feedback_event_from_model(m) for m in models]


async def get_feedback_summary(
    session: AsyncSession, *, repository_id: uuid.UUID | None = None
) -> list[FindingFeedbackSummary]:
    """One :class:`FindingFeedbackSummary` per finding with any feedback
    in ``repository_id`` (or globally if ``None``). A single raw-event
    query, grouped by ``finding_id`` in Python."""

    models = await FeedbackEventRepository().list_all(session, repository_id=repository_id)
    by_finding: dict[uuid.UUID, list[FeedbackEvent]] = defaultdict(list)
    for model in models:
        if model.finding_id is None:
            continue
        by_finding[model.finding_id].append(feedback_event_from_model(model))

    return [compute_finding_assessment(finding_id, events) for finding_id, events in by_finding.items()]


async def get_negative_feedback_findings(
    session: AsyncSession, *, repository_id: uuid.UUID | None = None
) -> list[FindingFeedbackSummary]:
    """``false_positive_candidates`` -- strongest available negative
    evidence, never ``confirmed_false_positives`` (Phase 9 spec section
    25)."""

    summaries = await get_feedback_summary(session, repository_id=repository_id)
    return [s for s in summaries if is_false_positive_candidate(s)]


async def get_positive_feedback_findings(
    session: AsyncSession, *, repository_id: uuid.UUID | None = None
) -> list[FindingFeedbackSummary]:
    """``high_value_candidates`` -- strongest available positive
    evidence, never ``guaranteed_true_positives`` (Phase 9 spec section
    26)."""

    summaries = await get_feedback_summary(session, repository_id=repository_id)
    return [s for s in summaries if is_high_value_candidate(s)]


async def recompute_and_persist_all(
    session: AsyncSession,
    *,
    repository_id: uuid.UUID | None = None,
    assessment_version: int = FEEDBACK_ASSESSMENT_VERSION,
) -> int:
    """Recompute every finding's :class:`~patchfrog.feedback.domain.FeedbackAssessment`
    from its current raw events and upsert it into ``feedback_assessments``
    under ``assessment_version``. Never touches or deletes raw events
    (Phase 9 spec section 36). Returns the number of assessments written."""

    summaries = await get_feedback_summary(session, repository_id=repository_id)
    repo = FeedbackAssessmentRepository()
    for summary in summaries:
        counts: dict[str, int | bool] = {
            "positive_reactions": summary.positive_reactions,
            "negative_reactions": summary.negative_reactions,
            "developer_replies": summary.developer_replies,
            "explicit_useful": summary.explicit_useful,
            "explicit_false_positive": summary.explicit_false_positive,
            "explicit_fixed": summary.explicit_fixed,
            "explicit_ignore": summary.explicit_ignore,
            "thread_resolved": summary.thread_resolved,
            "finding_changed": summary.finding_changed,
            "finding_disappeared": summary.finding_disappeared,
        }
        await repo.upsert(session, assessment=summary.assessment, counts=counts)
    return len(summaries)
