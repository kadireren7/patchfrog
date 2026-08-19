"""Persisted GitHub review-publication state (Phase 6).

Mirrors :mod:`patchfrog.persistence.models.review`'s shape: one canonical
publication row (``review_publications``) plus a cascade-deleted child
table of individual planned/published comments
(``review_publication_comments``). Canonical *published* identity is
``(review_run_id, mode)`` -- a review run has at most one successful
``PUBLISH``-mode publication, enforced the same way
``uq_review_runs_succeeded_identity`` enforces one canonical
:class:`~patchfrog.persistence.models.review.ReviewRunModel` per identity:
a partial unique index scoped to ``status = 'published'`` only, so
``DRY_RUN``/``FAILED``/``STALE`` attempts can accumulate freely as history
without ever blocking a later real publish.

``review_publication_comments`` deliberately stores a ``body_hash``, not
the comment body itself -- the body is fully reconstructible from the
already-persisted :class:`~patchfrog.persistence.models.review.AIFindingModel`
row it traces back to (via ``finding_id``); duplicating it here would just
be another place for the two to drift.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, Index, Integer, String, Text, func, text
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import Boolean, Uuid

from patchfrog.analysis.domain import Severity
from patchfrog.persistence.models._enum import enum_column
from patchfrog.persistence.models.base import Base
from patchfrog.publishing.domain import (
    PublicationDisposition,
    ReviewPublicationMode,
    ReviewPublicationStatus,
)


class ReviewPublicationModel(Base):
    """One publication attempt (dry-run or real) for one review run."""

    __tablename__ = "review_publications"
    __table_args__ = (
        Index("ix_review_publications_review_run_id", "review_run_id"),
        Index("ix_review_publications_repository_id", "repository_id"),
        Index(
            "uq_review_publications_published_identity",
            "review_run_id",
            "mode",
            unique=True,
            postgresql_where=text("status = 'published'"),
            sqlite_where=text("status = 'published'"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    review_run_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("review_runs.id", ondelete="CASCADE")
    )
    repository_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("repositories.id"))
    pull_request_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("pull_requests.id"), nullable=True
    )
    pull_request_number: Mapped[int] = mapped_column(Integer)
    base_sha: Mapped[str | None] = mapped_column(String(40), nullable=True)
    head_sha: Mapped[str] = mapped_column(String(40))
    mode: Mapped[ReviewPublicationMode] = mapped_column(enum_column(ReviewPublicationMode, length=16))
    status: Mapped[ReviewPublicationStatus] = mapped_column(enum_column(ReviewPublicationStatus, length=32))
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    github_review_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    inline_count: Mapped[int] = mapped_column(Integer, default=0)
    summary_only_count: Mapped[int] = mapped_column(Integer, default=0)
    omitted_count: Mapped[int] = mapped_column(Integer, default=0)
    reconciled: Mapped[bool] = mapped_column(Boolean, default=False)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ReviewPublicationCommentModel(Base):
    """One finding's disposition within a publication attempt -- every
    finding fed to the planner produces exactly one of these, whatever
    its disposition (inline / summary-only / omitted), so this table is
    the full audit trail of "what was intended", not just "what got
    published"."""

    __tablename__ = "review_publication_comments"
    __table_args__ = (
        Index("ix_review_publication_comments_publication_id", "review_publication_id"),
        Index(
            "uq_review_publication_comments_fingerprint",
            "review_publication_id",
            "fingerprint",
            unique=True,
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    review_publication_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("review_publications.id", ondelete="CASCADE")
    )
    finding_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("ai_findings.id", ondelete="CASCADE")
    )
    fingerprint: Mapped[str] = mapped_column(String(64))
    path: Mapped[str] = mapped_column(String(1024))
    severity: Mapped[Severity] = mapped_column(enum_column(Severity, length=16))
    disposition: Mapped[PublicationDisposition] = mapped_column(
        enum_column(PublicationDisposition, length=16)
    )
    reason: Mapped[str] = mapped_column(Text)
    side: Mapped[str | None] = mapped_column(String(8), nullable=True)
    line: Mapped[int | None] = mapped_column(Integer, nullable=True)
    start_side: Mapped[str | None] = mapped_column(String(8), nullable=True)
    start_line: Mapped[int | None] = mapped_column(Integer, nullable=True)
    body_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    github_comment_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
