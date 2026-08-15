from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import Uuid

from patchfrog.persistence.models._enum import enum_column
from patchfrog.persistence.models.base import Base


class IndexStatus(StrEnum):
    """Lifecycle status of a single repository indexing run."""

    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class RepositoryIndexModel(Base):
    """One indexing run of a repository at a specific commit SHA.

    ``is_active`` marks the current known-good index for a repository —
    at most one row per ``repository_id`` may have ``is_active = true``
    (enforced by a partial unique index below). A failed run never
    touches this flag, so a failed index can never replace or corrupt the
    last succeeded one; the flag only moves once a *new* run has fully
    succeeded (see :mod:`patchfrog.indexing.service`).
    """

    __tablename__ = "repository_indexes"
    __table_args__ = (
        Index(
            "uq_repository_indexes_repo_version",
            "repository_id",
            "index_version",
            unique=True,
        ),
        Index(
            "uq_repository_indexes_active_per_repo",
            "repository_id",
            unique=True,
            postgresql_where=text("is_active = true"),
            sqlite_where=text("is_active = 1"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    repository_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("repositories.id"), index=True)
    commit_sha: Mapped[str] = mapped_column(String(40))
    index_version: Mapped[int] = mapped_column(Integer)
    status: Mapped[IndexStatus] = mapped_column(enum_column(IndexStatus, length=32))
    is_active: Mapped[bool] = mapped_column(Boolean, default=False)

    files_total: Mapped[int] = mapped_column(Integer, default=0)
    files_parsed: Mapped[int] = mapped_column(Integer, default=0)
    files_failed: Mapped[int] = mapped_column(Integer, default=0)
    files_reused: Mapped[int] = mapped_column(Integer, default=0)
    symbols_extracted: Mapped[int] = mapped_column(Integer, default=0)
    edges_created: Mapped[int] = mapped_column(Integer, default=0)
    duration_ms: Mapped[float | None] = mapped_column(Float, nullable=True)

    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
