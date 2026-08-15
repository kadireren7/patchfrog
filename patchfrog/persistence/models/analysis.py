"""Persisted static-analysis tables for one analysis run.

Every child table is scoped by ``analysis_run_id`` and cascades on its
deletion, mirroring the ``repository_index_id``-scoped tables from Phase 2
(:mod:`patchfrog.persistence.models.code_index`).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import DateTime, Float, ForeignKey, Index, Integer, String, Text, func, text
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import Uuid

from patchfrog.analysis.domain import AnalyzerExecutionStatus, Confidence, FindingCategory, Severity
from patchfrog.persistence.models._enum import enum_column
from patchfrog.persistence.models.base import Base


class AnalysisRunStatus(StrEnum):
    """Lifecycle status of one analysis run.

    Determined purely by ``analyzers_failed``/``analyzers_succeeded``
    (``SKIPPED``/``UNSUPPORTED`` never count as a failure — a repository
    with no compile database, for instance, correctly skips clang-tidy
    without that being treated as anything going wrong):

    - ``SUCCEEDED``: ``analyzers_failed == 0`` — every analyzer that was
      attempted either succeeded, or none were attempted at all (e.g. no
      applicable language in scope).
    - ``PARTIAL``: ``analyzers_failed > 0`` and ``analyzers_succeeded > 0``
      — the run's findings are real but incomplete.
    - ``FAILED``: ``analyzers_failed > 0`` and ``analyzers_succeeded == 0``
      — nothing usable came out of this run — or the run crashed before
      completing.
    """

    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    PARTIAL = "partial"
    FAILED = "failed"


class FindingStatus(StrEnum):
    """Deliberately just one value for Phase 3 — see the module docstring
    of :mod:`patchfrog.analysis.domain` for why accepted/dismissed/fixed
    are not yet modeled."""

    ACTIVE = "active"


class AnalysisRunModel(Base):
    """One static-analysis run of a repository at a specific commit/index.

    ``repository_index_id`` ties every run to the exact Phase 2 index it
    was enriched against — see
    :meth:`patchfrog.analysis.service.StaticAnalysisService` for the
    "analysis commit SHA == repository index commit SHA" invariant this
    supports. A partial unique index enforces that only one *succeeded*
    run may exist per ``(repository_id, commit_sha, config_fingerprint)``
    — repeated identical requests reuse it rather than duplicating data;
    a failed/partial attempt never blocks a fresh retry.
    """

    __tablename__ = "analysis_runs"
    __table_args__ = (
        Index("ix_analysis_runs_repository_id", "repository_id"),
        Index("ix_analysis_runs_repository_index_id", "repository_index_id"),
        Index("ix_analysis_runs_pull_request_id", "pull_request_id"),
        Index(
            "uq_analysis_runs_succeeded_identity",
            "repository_id",
            "commit_sha",
            "config_fingerprint",
            unique=True,
            postgresql_where=text("status = 'succeeded'"),
            sqlite_where=text("status = 'succeeded'"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    repository_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("repositories.id"))
    repository_index_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("repository_indexes.id"))
    pull_request_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("pull_requests.id"), nullable=True
    )
    commit_sha: Mapped[str] = mapped_column(String(40))
    config_fingerprint: Mapped[str] = mapped_column(String(64))
    status: Mapped[AnalysisRunStatus] = mapped_column(enum_column(AnalysisRunStatus, length=16))

    analyzers_succeeded: Mapped[int] = mapped_column(Integer, default=0)
    analyzers_failed: Mapped[int] = mapped_column(Integer, default=0)
    analyzers_skipped: Mapped[int] = mapped_column(Integer, default=0)
    raw_findings_count: Mapped[int] = mapped_column(Integer, default=0)
    findings_count: Mapped[int] = mapped_column(Integer, default=0)
    duration_ms: Mapped[float | None] = mapped_column(Float, nullable=True)

    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AnalyzerExecutionModel(Base):
    """One analyzer's single execution within an analysis run."""

    __tablename__ = "analyzer_executions"
    __table_args__ = (Index("ix_analyzer_executions_analysis_run_id", "analysis_run_id"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    analysis_run_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("analysis_runs.id", ondelete="CASCADE")
    )
    analyzer: Mapped[str] = mapped_column(String(64))
    version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[AnalyzerExecutionStatus] = mapped_column(enum_column(AnalyzerExecutionStatus, length=16))
    duration_ms: Mapped[float] = mapped_column(Float)
    raw_findings_count: Mapped[int] = mapped_column(Integer, default=0)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class FindingModel(Base):
    """One deduplicated finding — the primary instance of a
    :class:`~patchfrog.analysis.dedupe.DeduplicatedFinding` group."""

    __tablename__ = "findings"
    __table_args__ = (
        Index("ix_findings_analysis_run_id", "analysis_run_id"),
        Index("ix_findings_file_path", "analysis_run_id", "file_path"),
        Index("ix_findings_fingerprint", "fingerprint"),
        Index("ix_findings_severity", "analysis_run_id", "severity"),
        Index("ix_findings_symbol_id", "symbol_id"),
        Index("ix_findings_changed_line", "analysis_run_id", "is_on_changed_line"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    analysis_run_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("analysis_runs.id", ondelete="CASCADE")
    )
    fingerprint: Mapped[str] = mapped_column(String(64))
    rule_id: Mapped[str] = mapped_column(String(128))
    category: Mapped[FindingCategory] = mapped_column(enum_column(FindingCategory, length=32))
    title: Mapped[str] = mapped_column(String(512))
    message: Mapped[str] = mapped_column(Text)
    severity: Mapped[Severity] = mapped_column(enum_column(Severity, length=16))
    confidence: Mapped[Confidence] = mapped_column(enum_column(Confidence, length=16))

    file_path: Mapped[str] = mapped_column(String(1024))
    start_line: Mapped[int] = mapped_column(Integer)
    end_line: Mapped[int] = mapped_column(Integer)
    start_column: Mapped[int] = mapped_column(Integer)
    end_column: Mapped[int] = mapped_column(Integer)

    symbol_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("symbols.id", ondelete="SET NULL"), nullable=True
    )
    symbol_name: Mapped[str | None] = mapped_column(String(512), nullable=True)

    source_analyzer: Mapped[str] = mapped_column(String(64))

    is_on_changed_line: Mapped[bool] = mapped_column(default=False)
    is_in_changed_symbol: Mapped[bool] = mapped_column(default=False)

    status: Mapped[FindingStatus] = mapped_column(
        enum_column(FindingStatus, length=16), default=FindingStatus.ACTIVE
    )
    raw_metadata: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class FindingSourceModel(Base):
    """One analyzer's individual report that was merged into a
    :class:`FindingModel` — preserves full per-analyzer provenance."""

    __tablename__ = "finding_sources"
    __table_args__ = (Index("ix_finding_sources_finding_id", "finding_id"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    finding_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("findings.id", ondelete="CASCADE"))
    analyzer: Mapped[str] = mapped_column(String(64))
    rule_id: Mapped[str] = mapped_column(String(128))
    fingerprint: Mapped[str] = mapped_column(String(64))
    severity: Mapped[Severity] = mapped_column(enum_column(Severity, length=16))
    confidence: Mapped[Confidence] = mapped_column(enum_column(Confidence, length=16))
    message: Mapped[str] = mapped_column(Text)
    start_line: Mapped[int] = mapped_column(Integer)
    end_line: Mapped[int] = mapped_column(Integer)
    start_column: Mapped[int] = mapped_column(Integer)
    end_column: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
