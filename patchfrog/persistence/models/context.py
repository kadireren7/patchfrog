"""Persisted Context Engine tables for one generated context bundle.

Mirrors :mod:`patchfrog.persistence.models.analysis`'s shape: a canonical
run row (``context_bundles``) plus its child items, cascade-deleted
together. Canonical-bundle identity is
``(repository_id, commit_sha, target_fingerprint, config_fingerprint)`` --
``config_fingerprint`` already folds in ``CONTEXT_ENGINE_VERSION`` (see
:meth:`patchfrog.context.config.ContextConfig.fingerprint`), so a version
bump alone invalidates reuse of prior bundles, the same way Phase 3's
``toolchain_fingerprint`` does for analysis runs.

Item ``content`` is stored directly rather than reconstructed on read
from ``(location, content_hash)`` against a live snapshot: items are
already bounded by the budgeter (a few hundred lines/tokens at most per
bundle), so the storage cost is small and fixed, while reconstruction
would require re-acquiring a repository snapshot (a network fetch) for
every read -- far more complexity for a saving that doesn't matter at
this size. ``content_hash`` is still stored alongside it so a caller can
cheaply detect whether stored content matches what's on disk today
without re-reading it.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import DateTime, Float, ForeignKey, Index, Integer, String, Text, func, text
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import Boolean, Uuid

from patchfrog.context.domain import ContextItemKind, ContextRelationship, ContextTargetType
from patchfrog.persistence.models._enum import enum_column
from patchfrog.persistence.models.base import Base


class ContextBundleStatus(StrEnum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class ContextBundleModel(Base):
    """One canonical context-generation run for one target."""

    __tablename__ = "context_bundles"
    __table_args__ = (
        Index("ix_context_bundles_repository_id", "repository_id"),
        Index("ix_context_bundles_repository_index_id", "repository_index_id"),
        Index("ix_context_bundles_analysis_run_id", "analysis_run_id"),
        Index("ix_context_bundles_finding_id", "finding_id"),
        Index("ix_context_bundles_target_symbol_id", "target_symbol_id"),
        Index(
            "uq_context_bundles_succeeded_identity",
            "repository_id",
            "commit_sha",
            "target_fingerprint",
            "config_fingerprint",
            unique=True,
            postgresql_where=text("status = 'succeeded'"),
            sqlite_where=text("status = 'succeeded'"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    repository_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("repositories.id"))
    repository_index_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("repository_indexes.id"))
    analysis_run_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("analysis_runs.id"), nullable=True
    )
    finding_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("findings.id"), nullable=True)
    target_symbol_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("symbols.id", ondelete="SET NULL"), nullable=True
    )

    commit_sha: Mapped[str] = mapped_column(String(40))
    target_type: Mapped[ContextTargetType] = mapped_column(enum_column(ContextTargetType, length=16))
    target_file_path: Mapped[str] = mapped_column(String(1024))
    target_line: Mapped[int | None] = mapped_column(Integer, nullable=True)
    target_fingerprint: Mapped[str] = mapped_column(String(64))
    config_fingerprint: Mapped[str] = mapped_column(String(64))
    engine_version: Mapped[int] = mapped_column(Integer)
    status: Mapped[ContextBundleStatus] = mapped_column(enum_column(ContextBundleStatus, length=16))

    candidate_count: Mapped[int] = mapped_column(Integer, default=0)
    selected_count: Mapped[int] = mapped_column(Integer, default=0)
    dropped_budget: Mapped[int] = mapped_column(Integer, default=0)
    dropped_overlap: Mapped[int] = mapped_column(Integer, default=0)
    dropped_duplicate: Mapped[int] = mapped_column(Integer, default=0)
    total_tokens: Mapped[int] = mapped_column(Integer, default=0)
    total_lines: Mapped[int] = mapped_column(Integer, default=0)
    generation_ms: Mapped[float | None] = mapped_column(Float, nullable=True)

    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ContextItemModel(Base):
    """One selected, ranked, budgeted piece of context within a bundle."""

    __tablename__ = "context_items"
    __table_args__ = (
        Index("ix_context_items_bundle_id", "bundle_id"),
        Index("ix_context_items_symbol_id", "symbol_id"),
        Index("ix_context_items_kind", "bundle_id", "kind"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    bundle_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("context_bundles.id", ondelete="CASCADE"))
    rank: Mapped[int] = mapped_column(Integer)
    kind: Mapped[ContextItemKind] = mapped_column(enum_column(ContextItemKind, length=32))
    relationship: Mapped[ContextRelationship] = mapped_column(enum_column(ContextRelationship, length=32))
    distance: Mapped[int] = mapped_column(Integer)

    file_path: Mapped[str] = mapped_column(String(1024))
    symbol_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("symbols.id", ondelete="SET NULL"), nullable=True
    )
    symbol_name: Mapped[str | None] = mapped_column(String(512), nullable=True)
    qualified_name: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    start_line: Mapped[int] = mapped_column(Integer)
    end_line: Mapped[int] = mapped_column(Integer)

    content: Mapped[str] = mapped_column(Text)
    content_hash: Mapped[str] = mapped_column(String(64))
    truncated: Mapped[bool] = mapped_column(Boolean, default=False)

    score: Mapped[float] = mapped_column(Float)
    score_breakdown: Mapped[str] = mapped_column(Text)
    estimated_tokens: Mapped[int] = mapped_column(Integer)
    reason: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
