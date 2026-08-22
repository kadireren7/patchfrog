"""Persisted feedback loop state (Phase 9).

Two tables, mirroring the raw/derived split every other engine in
PatchFrog uses (``ai_finding_proposals``/``ai_findings``,
``review_memory_findings``/``review_memory_transitions``):

- ``feedback_events`` is the append-only, immutable audit trail of every
  raw signal PatchFrog ever observed -- a reaction, a reply, an explicit
  command, a thread transition, a Phase 7 code-lifecycle signal, a PR
  lifecycle event. Nothing here is ever updated or deleted once ingested
  (see :mod:`patchfrog.feedback.domain`'s module docstring). Idempotency
  is enforced by ``uq_feedback_events_external_identity`` -- GitHub sync
  runs and retries must never create duplicate rows for the same
  underlying signal.
- ``feedback_assessments`` is the derived, *recomputable*
  :class:`~patchfrog.feedback.domain.FeedbackAssessment` for one finding
  at one assessment-rule version -- a full recompute overwrites this row
  for that ``(finding_id, assessment_version)`` pair, never the raw
  events it was computed from (see
  :func:`patchfrog.feedback.assessment.compute_finding_assessment`).

Foreign keys into ``ai_findings``/``review_publication_comments`` use
``ondelete="SET NULL"`` on ``feedback_events`` (never CASCADE) --
preserving feedback *history* even if the finding it was about is later
gone is a tombstone requirement (Phase 9 spec section 43), not a "this
data no longer matters" situation. ``feedback_assessments`` is pure
derived data and cascades normally: it can always be recomputed from
surviving raw events.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Index, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import Uuid

from patchfrog.feedback.domain import (
    FEEDBACK_ASSESSMENT_VERSION,
    FEEDBACK_ENGINE_VERSION,
    FeedbackEventType,
    FeedbackSource,
    ResolutionState,
    SignalPolarity,
    SignalStrength,
)
from patchfrog.persistence.models._enum import enum_column
from patchfrog.persistence.models.base import Base


class FeedbackEventModel(Base):
    """One raw, immutable feedback signal."""

    __tablename__ = "feedback_events"
    __table_args__ = (
        Index("ix_feedback_events_repository_id", "repository_id"),
        Index("ix_feedback_events_pull_request_id", "pull_request_id"),
        Index("ix_feedback_events_finding_id", "finding_id"),
        Index("ix_feedback_events_review_publication_comment_id", "review_publication_comment_id"),
        Index(
            "uq_feedback_events_external_identity",
            "source",
            "event_type",
            "external_event_id",
            unique=True,
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    repository_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("repositories.id"))
    pull_request_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("pull_requests.id", ondelete="SET NULL"), nullable=True
    )
    review_run_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("review_runs.id", ondelete="SET NULL"), nullable=True
    )
    publication_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("review_publications.id", ondelete="SET NULL"), nullable=True
    )
    review_publication_comment_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("review_publication_comments.id", ondelete="SET NULL"), nullable=True
    )
    finding_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("ai_findings.id", ondelete="SET NULL"), nullable=True
    )
    github_review_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    github_comment_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    event_type: Mapped[FeedbackEventType] = mapped_column(enum_column(FeedbackEventType, length=32))
    source: Mapped[FeedbackSource] = mapped_column(enum_column(FeedbackSource, length=32))
    #: Stable identity of the underlying GitHub (or Phase 7 lifecycle)
    #: signal -- unique together with ``source``/``event_type``. See the
    #: module docstring and :mod:`patchfrog.feedback.sync` for the exact
    #: per-event-type convention (e.g. ``reaction:<github_reaction_id>``).
    external_event_id: Mapped[str] = mapped_column(String(256))
    raw_signal: Mapped[str] = mapped_column(Text)
    normalized_signal: Mapped[str] = mapped_column(String(64))
    signal_strength: Mapped[SignalStrength] = mapped_column(enum_column(SignalStrength, length=16))
    actor_login: Mapped[str] = mapped_column(String(255))
    actor_is_bot: Mapped[bool] = mapped_column(Boolean)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    ingested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    #: JSON-serialized ``dict[str, str]`` -- named to avoid colliding with
    #: SQLAlchemy's reserved ``Base.metadata`` attribute.
    event_metadata: Mapped[str] = mapped_column(Text, default="{}")
    engine_version: Mapped[int] = mapped_column(Integer, default=FEEDBACK_ENGINE_VERSION)


class FeedbackAssessmentModel(Base):
    """One finding's derived, recomputable feedback assessment at one
    assessment-rule version."""

    __tablename__ = "feedback_assessments"
    __table_args__ = (
        Index(
            "uq_feedback_assessments_finding_version",
            "finding_id",
            "assessment_version",
            unique=True,
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    finding_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("ai_findings.id", ondelete="CASCADE"))
    assessment_version: Mapped[int] = mapped_column(Integer, default=FEEDBACK_ASSESSMENT_VERSION)
    usefulness_signal: Mapped[SignalPolarity] = mapped_column(enum_column(SignalPolarity, length=16))
    correctness_signal: Mapped[SignalPolarity] = mapped_column(enum_column(SignalPolarity, length=16))
    resolution_signal: Mapped[ResolutionState] = mapped_column(enum_column(ResolutionState, length=16))
    engagement_signal: Mapped[bool] = mapped_column(Boolean)
    confidence: Mapped[SignalStrength | None] = mapped_column(enum_column(SignalStrength, length=16), nullable=True)
    reasons: Mapped[str] = mapped_column(Text, default="[]")
    positive_reactions: Mapped[int] = mapped_column(Integer, default=0)
    negative_reactions: Mapped[int] = mapped_column(Integer, default=0)
    developer_replies: Mapped[int] = mapped_column(Integer, default=0)
    explicit_useful: Mapped[int] = mapped_column(Integer, default=0)
    explicit_false_positive: Mapped[int] = mapped_column(Integer, default=0)
    explicit_fixed: Mapped[int] = mapped_column(Integer, default=0)
    explicit_ignore: Mapped[int] = mapped_column(Integer, default=0)
    thread_resolved: Mapped[bool] = mapped_column(Boolean, default=False)
    finding_changed: Mapped[bool] = mapped_column(Boolean, default=False)
    finding_disappeared: Mapped[bool] = mapped_column(Boolean, default=False)
    computed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
