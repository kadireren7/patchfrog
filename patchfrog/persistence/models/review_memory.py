"""Persisted PR-scoped incremental review memory (Phase 7).

Three tables:

``review_generations`` -- one row per review run that participates in a
PR's incremental sequence (only ``SUCCEEDED`` review runs ever get one;
a partial/failed run simply never joins the chain -- see the module
docstring of :mod:`patchfrog.review_memory.service`). Forms an explicit
linked list via ``previous_generation_id``, decided once (at creation
time, under ancestry proof) and never re-derived by later queries.

``review_memory_findings`` -- one *mutable* row per continuously-tracked
logical finding, updated in place as it survives across review
generations (moves lines, gets reconfirmed, etc.) -- not one row per
generation. ``review_memory_transitions`` is the append-only audit trail
of every status change a finding ever underwent.

Cascade policy: deleting a ``pull_requests`` row cascades through
``review_generations`` (FK'd to it) to ``review_memory_findings`` and
transitively to ``review_memory_transitions`` -- a PR's memory is a
self-contained unit. Deleting one ``review_runs`` row does **not**
cascade-delete ``review_memory_findings`` (they must survive their
originating run being cleaned up for unrelated reasons); it does cascade
``review_generations`` (a generation has no meaning without its run) and
``review_memory_transitions`` referencing that run as their target.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text, func, text
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import Boolean, Uuid

from patchfrog.analysis.domain import FindingCategory, Severity
from patchfrog.domain.code import SymbolKind
from patchfrog.persistence.models._enum import enum_column
from patchfrog.persistence.models.base import Base
from patchfrog.review_memory.domain import (
    FindingMemoryStatus,
    IncrementalRunMode,
    TransitionReasonCode,
)


class ReviewGenerationModel(Base):
    """One review run's place in a PR's incremental sequence."""

    __tablename__ = "review_generations"
    __table_args__ = (
        Index("ix_review_generations_repo_pr", "repository_id", "pull_request_id"),
        Index("ix_review_generations_review_run_id", "review_run_id", unique=True),
        Index("ix_review_generations_previous", "previous_generation_id"),
        Index(
            "uq_review_generations_pr_sequence", "pull_request_id", "sequence_number", unique=True
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    repository_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("repositories.id"))
    pull_request_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("pull_requests.id", ondelete="CASCADE")
    )
    #: 1-based, strictly increasing per ``pull_request_id`` -- the *only*
    #: thing :meth:`~patchfrog.persistence.repositories.review_generation.ReviewGenerationRepository.get_latest_for_pr`
    #: orders by. ``created_at`` alone is not a safe "most recent" key:
    #: two generations for the same PR can legitimately be created within
    #: the same timestamp tick (SQLite's default timestamp resolution is
    #: whole seconds; even Postgres's microsecond resolution is not a
    #: real uniqueness guarantee), which would make "the previous
    #: generation" a coin flip between two genuinely different reviews --
    #: exactly the ambiguity the Phase 7 spec requires resolving to
    #: "fresh review", never guessed. Assigned the same way
    #: ``RepositoryIndexModel.index_version`` is (``MAX(...)+1`` under a
    #: transaction-scoped advisory lock keyed on ``pull_request_id``).
    sequence_number: Mapped[int] = mapped_column(Integer)
    review_run_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("review_runs.id", ondelete="CASCADE")
    )
    commit_sha: Mapped[str] = mapped_column(String(40))
    previous_generation_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("review_generations.id", ondelete="SET NULL"), nullable=True
    )
    previous_commit_sha: Mapped[str | None] = mapped_column(String(40), nullable=True)
    ancestry_verified: Mapped[bool] = mapped_column(Boolean)
    mode: Mapped[IncrementalRunMode] = mapped_column(enum_column(IncrementalRunMode, length=16))
    compatibility_ok: Mapped[bool] = mapped_column(Boolean)
    invalidation_reason: Mapped[TransitionReasonCode | None] = mapped_column(
        enum_column(TransitionReasonCode, length=32), nullable=True
    )
    memory_compatibility_fingerprint: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ReviewMemoryFindingModel(Base):
    """One continuously-tracked logical finding for a PR -- mutable,
    updated in place across generations. See the module docstring."""

    __tablename__ = "review_memory_findings"
    __table_args__ = (
        Index("ix_review_memory_findings_repo_pr", "repository_id", "pull_request_id"),
        Index("ix_review_memory_findings_status", "pull_request_id", "status"),
        Index(
            "uq_review_memory_findings_active_family",
            "pull_request_id",
            "semantic_family_fingerprint",
            unique=True,
            # Only one "live" tracked instance per semantic family per PR
            # at a time -- a resolved/superseded family can be re-opened
            # fresh (a new active row) without conflict.
            postgresql_where=text("status IN ('open', 'carried_forward', 'changed', 'ambiguous')"),
            sqlite_where=text("status IN ('open', 'carried_forward', 'changed', 'ambiguous')"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    repository_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("repositories.id"))
    pull_request_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("pull_requests.id", ondelete="CASCADE")
    )
    source_review_run_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("review_runs.id"))
    source_finding_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("ai_findings.id", ondelete="CASCADE"))
    current_finding_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("ai_findings.id", ondelete="SET NULL"), nullable=True
    )
    current_review_run_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("review_runs.id"), nullable=True
    )
    first_seen_commit_sha: Mapped[str] = mapped_column(String(40))
    last_seen_commit_sha: Mapped[str] = mapped_column(String(40))
    file_path: Mapped[str] = mapped_column(String(1024))
    symbol_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("symbols.id", ondelete="SET NULL"), nullable=True
    )
    symbol_qualified_name: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    symbol_kind: Mapped[SymbolKind | None] = mapped_column(enum_column(SymbolKind, length=16), nullable=True)
    category: Mapped[FindingCategory] = mapped_column(enum_column(FindingCategory, length=32))
    severity: Mapped[Severity] = mapped_column(enum_column(Severity, length=16))
    title: Mapped[str] = mapped_column(String(512))
    message: Mapped[str] = mapped_column(Text)
    start_line: Mapped[int] = mapped_column(Integer)
    end_line: Mapped[int] = mapped_column(Integer)
    exact_fingerprint: Mapped[str] = mapped_column(String(64))
    semantic_family_fingerprint: Mapped[str] = mapped_column(String(64))
    status: Mapped[FindingMemoryStatus] = mapped_column(enum_column(FindingMemoryStatus, length=16))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class ReviewMemoryTransitionModel(Base):
    """Append-only audit trail of every status change a
    :class:`ReviewMemoryFindingModel` ever underwent."""

    __tablename__ = "review_memory_transitions"
    __table_args__ = (
        Index("ix_review_memory_transitions_finding", "memory_finding_id"),
        Index("ix_review_memory_transitions_target_run", "target_review_run_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    memory_finding_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("review_memory_findings.id", ondelete="CASCADE")
    )
    source_review_run_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("review_runs.id"), nullable=True
    )
    target_review_run_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("review_runs.id"))
    old_status: Mapped[FindingMemoryStatus | None] = mapped_column(
        enum_column(FindingMemoryStatus, length=16), nullable=True
    )
    new_status: Mapped[FindingMemoryStatus] = mapped_column(enum_column(FindingMemoryStatus, length=16))
    reason: Mapped[TransitionReasonCode] = mapped_column(enum_column(TransitionReasonCode, length=32))
    detail: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
