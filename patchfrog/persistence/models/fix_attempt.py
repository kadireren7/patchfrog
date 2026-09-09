"""Persisted Fix Attempt table -- Milestone T (T3).

One new table, justified because verification is asynchronous (dispatched
to the existing S6 verifier queue exactly like a review candidate), an MCP
client may reconnect and poll status, and idempotency needs a durable
identity -- see ``validation/agent_handoff/latest-summary.md`` section 2.3
for the full "why a table" reasoning (Part AG explicitly warns against
adding one for architecture aesthetics alone).
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import Uuid

from patchfrog.fix_verification.domain import FixAttemptStatus
from patchfrog.persistence.models._enum import enum_column
from patchfrog.persistence.models.base import Base


class FixAttemptModel(Base):
    """One attempt to fix a specific PatchFrog finding, identified by an
    exact new commit SHA. Identity is ``(handoff_id,
    candidate_fix_commit_sha)`` -- a repeat request for the same pair
    always returns this same row (Part AF idempotency), never a duplicate
    concurrent verification."""

    __tablename__ = "fix_attempts"
    __table_args__ = (
        UniqueConstraint(
            "handoff_id", "candidate_fix_commit_sha", name="uq_fix_attempts_handoff_candidate"
        ),
        Index("ix_fix_attempts_finding_id", "finding_id"),
        Index("ix_fix_attempts_repository_id_status", "repository_id", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    #: The originating handoff's own deterministic identity (see
    #: patchfrog.agent_handoff.domain.compute_handoff_id) -- not a foreign
    #: key, since a handoff is a derived projection, never a persisted row
    #: of its own (Part I: "avoid a new table unless necessary" applied to
    #: the handoff itself too).
    handoff_id: Mapped[str] = mapped_column(String(64))
    finding_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("ai_findings.id", ondelete="CASCADE"))
    repository_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("repositories.id", ondelete="CASCADE"))
    original_commit_sha: Mapped[str] = mapped_column(String(40))
    candidate_fix_commit_sha: Mapped[str] = mapped_column(String(40))
    status: Mapped[FixAttemptStatus] = mapped_column(enum_column(FixAttemptStatus, length=16))

    #: JSON array of short plain-English evidence labels -- never a log,
    #: never internal chain-of-thought (see FixVerificationResult).
    deterministic_evidence: Mapped[str] = mapped_column(Text, default="[]")
    executable_verification_outcome: Mapped[str | None] = mapped_column(String(32), nullable=True)
    remaining_issue_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    limitations: Mapped[str] = mapped_column(Text, default="[]")
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
