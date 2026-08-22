"""Persistence operations for the feedback loop (Phase 9).

``FeedbackEventRepository.create_if_new`` is the idempotency guard for
raw event ingestion -- see :mod:`patchfrog.feedback.sync`, which may
insert many events within one session per sync run. Each attempt runs in
its own ``SAVEPOINT`` (``session.begin_nested()``) so one duplicate
(caught by ``uq_feedback_events_external_identity``) only rolls back that
one insert, never the rest of the batch already flushed in the same
outer transaction -- a plain ``session.rollback()`` (the idiom used for
single-shot idempotency guards elsewhere, e.g.
:meth:`patchfrog.persistence.repositories.pull_request_ingestion.PullRequestIngestionRepository.reserve`)
would be wrong here precisely because this one is called in a loop over
one shared session.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from patchfrog.feedback.domain import (
    ActorIdentity,
    FeedbackAssessment,
    FeedbackEvent,
    FeedbackEventType,
)
from patchfrog.persistence.models.feedback import FeedbackAssessmentModel, FeedbackEventModel


class FeedbackEventRepository:
    async def create_if_new(
        self, session: AsyncSession, *, event: FeedbackEvent
    ) -> FeedbackEventModel | None:
        """Insert one raw event, or return ``None`` if an event with the
        same ``(source, event_type, external_event_id)`` identity was
        already ingested -- the caller should treat that as a no-op
        duplicate, never an error (GitHub sync runs are always safe to
        re-run)."""

        model = FeedbackEventModel(
            repository_id=event.repository_id,
            pull_request_id=event.pull_request_id,
            review_run_id=event.review_run_id,
            publication_id=event.publication_id,
            review_publication_comment_id=event.review_publication_comment_id,
            finding_id=event.finding_id,
            github_review_id=event.github_review_id,
            github_comment_id=event.github_comment_id,
            event_type=event.event_type,
            source=event.source,
            external_event_id=event.external_event_id,
            raw_signal=event.raw_signal,
            normalized_signal=event.normalized_signal,
            signal_strength=event.signal_strength,
            actor_login=event.actor.login,
            actor_is_bot=event.actor.is_bot,
            occurred_at=event.occurred_at,
            event_metadata=json.dumps(event.metadata),
        )
        try:
            async with session.begin_nested():
                session.add(model)
                await session.flush()
        except IntegrityError:
            return None
        return model

    async def list_for_finding(
        self, session: AsyncSession, *, finding_id: uuid.UUID
    ) -> list[FeedbackEventModel]:
        result = await session.execute(
            select(FeedbackEventModel)
            .where(FeedbackEventModel.finding_id == finding_id)
            .order_by(FeedbackEventModel.occurred_at)
        )
        return list(result.scalars().all())

    async def list_for_pull_request(
        self, session: AsyncSession, *, pull_request_id: uuid.UUID
    ) -> list[FeedbackEventModel]:
        result = await session.execute(
            select(FeedbackEventModel)
            .where(FeedbackEventModel.pull_request_id == pull_request_id)
            .order_by(FeedbackEventModel.occurred_at)
        )
        return list(result.scalars().all())

    async def list_for_review_run(
        self, session: AsyncSession, *, review_run_id: uuid.UUID
    ) -> list[FeedbackEventModel]:
        result = await session.execute(
            select(FeedbackEventModel)
            .where(FeedbackEventModel.review_run_id == review_run_id)
            .order_by(FeedbackEventModel.occurred_at)
        )
        return list(result.scalars().all())

    async def list_unattributed(
        self, session: AsyncSession, *, repository_id: uuid.UUID | None = None
    ) -> list[FeedbackEventModel]:
        """Events that could not be attributed to an exact finding (see
        :mod:`patchfrog.feedback.attribution`) -- still persisted for
        audit, never silently dropped."""

        stmt = select(FeedbackEventModel).where(FeedbackEventModel.finding_id.is_(None))
        if repository_id is not None:
            stmt = stmt.where(FeedbackEventModel.repository_id == repository_id)
        result = await session.execute(stmt.order_by(FeedbackEventModel.occurred_at))
        return list(result.scalars().all())

    async def distinct_finding_ids_with_feedback(
        self, session: AsyncSession, *, repository_id: uuid.UUID | None = None
    ) -> list[uuid.UUID]:
        stmt = select(FeedbackEventModel.finding_id).where(FeedbackEventModel.finding_id.is_not(None)).distinct()
        if repository_id is not None:
            stmt = stmt.where(FeedbackEventModel.repository_id == repository_id)
        result = await session.execute(stmt)
        return [row[0] for row in result.all()]

    async def list_all(
        self, session: AsyncSession, *, repository_id: uuid.UUID | None = None, since: object = None
    ) -> list[FeedbackEventModel]:
        stmt = select(FeedbackEventModel)
        if repository_id is not None:
            stmt = stmt.where(FeedbackEventModel.repository_id == repository_id)
        if since is not None:
            stmt = stmt.where(FeedbackEventModel.occurred_at >= since)
        result = await session.execute(stmt.order_by(FeedbackEventModel.occurred_at))
        return list(result.scalars().all())


def feedback_event_from_model(model: FeedbackEventModel) -> FeedbackEvent:
    return FeedbackEvent(
        repository_id=model.repository_id,
        pull_request_id=model.pull_request_id,
        review_run_id=model.review_run_id,
        publication_id=model.publication_id,
        review_publication_comment_id=model.review_publication_comment_id,
        finding_id=model.finding_id,
        github_review_id=model.github_review_id,
        github_comment_id=model.github_comment_id,
        event_type=FeedbackEventType(model.event_type),
        source=model.source,
        external_event_id=model.external_event_id,
        raw_signal=model.raw_signal,
        normalized_signal=model.normalized_signal,
        signal_strength=model.signal_strength,
        actor=ActorIdentity(login=model.actor_login, is_bot=model.actor_is_bot),
        occurred_at=model.occurred_at,
        metadata=json.loads(model.event_metadata) if model.event_metadata else {},
    )


class FeedbackAssessmentRepository:
    async def upsert(
        self, session: AsyncSession, *, assessment: FeedbackAssessment, counts: dict[str, int | bool]
    ) -> FeedbackAssessmentModel:
        """Overwrite the derived assessment row for
        ``(finding_id, assessment_version)`` -- never touches raw events.
        Safe to call repeatedly (recompute)."""

        existing = (
            await session.execute(
                select(FeedbackAssessmentModel).where(
                    FeedbackAssessmentModel.finding_id == assessment.finding_id,
                    FeedbackAssessmentModel.assessment_version == assessment.assessment_version,
                )
            )
        ).scalar_one_or_none()

        model = existing or FeedbackAssessmentModel(
            finding_id=assessment.finding_id, assessment_version=assessment.assessment_version
        )
        model.usefulness_signal = assessment.usefulness_signal
        model.correctness_signal = assessment.correctness_signal
        model.resolution_signal = assessment.resolution_signal
        model.engagement_signal = assessment.engagement_signal
        model.confidence = assessment.confidence
        model.reasons = json.dumps(list(assessment.reasons))
        model.positive_reactions = int(counts.get("positive_reactions", 0))
        model.negative_reactions = int(counts.get("negative_reactions", 0))
        model.developer_replies = int(counts.get("developer_replies", 0))
        model.explicit_useful = int(counts.get("explicit_useful", 0))
        model.explicit_false_positive = int(counts.get("explicit_false_positive", 0))
        model.explicit_fixed = int(counts.get("explicit_fixed", 0))
        model.explicit_ignore = int(counts.get("explicit_ignore", 0))
        model.thread_resolved = bool(counts.get("thread_resolved", False))
        model.finding_changed = bool(counts.get("finding_changed", False))
        model.finding_disappeared = bool(counts.get("finding_disappeared", False))
        if existing is None:
            session.add(model)
        await session.flush()
        return model

    async def get(
        self, session: AsyncSession, *, finding_id: uuid.UUID, assessment_version: int
    ) -> FeedbackAssessmentModel | None:
        return (
            await session.execute(
                select(FeedbackAssessmentModel).where(
                    FeedbackAssessmentModel.finding_id == finding_id,
                    FeedbackAssessmentModel.assessment_version == assessment_version,
                )
            )
        ).scalar_one_or_none()

    async def list_for_findings(
        self, session: AsyncSession, *, finding_ids: Sequence[uuid.UUID], assessment_version: int
    ) -> list[FeedbackAssessmentModel]:
        if not finding_ids:
            return []
        result = await session.execute(
            select(FeedbackAssessmentModel).where(
                FeedbackAssessmentModel.finding_id.in_(finding_ids),
                FeedbackAssessmentModel.assessment_version == assessment_version,
            )
        )
        return list(result.scalars().all())
