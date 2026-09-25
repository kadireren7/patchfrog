"""External dependency + contract registry (M5.5).

Three tables, all cascading from ``repositories`` (deleting a repository
removes its registry; nothing else references these rows yet):

- ``external_dependencies`` -- the *latest observed state* of one
  dependency per ``(repository_id, dependency_key)``. Never duplicated:
  repeated identical discovery only refreshes ``last_observed_*``. A
  dependency that disappears is marked ``removed`` (history kept), and
  reactivated if it reappears.
- ``external_dependency_usage_sites`` -- the dependency's *current* usage
  sites (replaced only when the set actually changes).
- ``external_contract_snapshots`` -- contract *history*: one row per
  distinct normalized-contract fingerprint ever observed for a
  dependency, with first/last observation. The input M6 will diff.

No raw source, no spec text, no secret value is stored: only controlled
tokens and the bounded normalized contract structure.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import Uuid

from patchfrog.persistence.models.base import Base


class ExternalDependencyModel(Base):
    __tablename__ = "external_dependencies"
    __table_args__ = (
        UniqueConstraint("repository_id", "dependency_key", name="uq_external_dependencies_repo_key"),
        Index("ix_external_dependencies_repo_status", "repository_id", "status"),
        Index("ix_external_dependencies_provider", "provider_key"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    repository_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("repositories.id", ondelete="CASCADE"))
    dependency_key: Mapped[str] = mapped_column(String(512))
    provider_key: Mapped[str] = mapped_column(String(255))
    display_name: Mapped[str] = mapped_column(String(255))
    kind: Mapped[str] = mapped_column(String(32))
    ecosystem: Mapped[str] = mapped_column(String(16))
    package_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    declared_version: Mapped[str | None] = mapped_column(String(128), nullable=True)
    resolved_version: Mapped[str | None] = mapped_column(String(128), nullable=True)
    confidence: Mapped[str] = mapped_column(String(8))
    contract_source_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    contract_source_ref: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    contract_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    usage_site_count: Mapped[int] = mapped_column(Integer, default=0)
    #: sha256 over the current usage-site set -- lets a repeated identical
    #: discovery skip rewriting the usage-site rows entirely.
    usage_fingerprint: Mapped[str] = mapped_column(String(64))
    evidence_counts: Mapped[str] = mapped_column(Text, default="{}")
    metadata_json: Mapped[str] = mapped_column(Text, default="{}")
    #: ``active`` | ``removed``.
    status: Mapped[str] = mapped_column(String(16), default="active")
    discovery_version: Mapped[int] = mapped_column(Integer)
    first_observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_observed_commit_sha: Mapped[str | None] = mapped_column(String(64), nullable=True)
    removed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ExternalDependencyUsageSiteModel(Base):
    __tablename__ = "external_dependency_usage_sites"
    __table_args__ = (
        UniqueConstraint(
            "dependency_id", "file_path", "line", "evidence_type", "token",
            name="uq_external_dependency_usage_sites_identity",
        ),
        Index("ix_external_dependency_usage_sites_dependency", "dependency_id"),
        Index("ix_external_dependency_usage_sites_file", "file_path"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    dependency_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("external_dependencies.id", ondelete="CASCADE")
    )
    file_path: Mapped[str] = mapped_column(String(1024))
    #: 0 = file-level (e.g. a manifest-free import-less reference); never
    #: NULL, so the unique constraint really de-duplicates.
    line: Mapped[int] = mapped_column(Integer, default=0)
    symbol: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    evidence_type: Mapped[str] = mapped_column(String(32))
    token: Mapped[str] = mapped_column(String(512))
    confidence: Mapped[str] = mapped_column(String(8))


class ExternalContractSnapshotModel(Base):
    __tablename__ = "external_contract_snapshots"
    __table_args__ = (
        UniqueConstraint(
            "dependency_id", "fingerprint", "normalization_version",
            name="uq_external_contract_snapshots_identity",
        ),
        Index("ix_external_contract_snapshots_dependency", "dependency_id", "last_observed_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    dependency_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("external_dependencies.id", ondelete="CASCADE")
    )
    fingerprint: Mapped[str] = mapped_column(String(64))
    algorithm: Mapped[str] = mapped_column(String(16))
    normalization_version: Mapped[int] = mapped_column(Integer)
    source_type: Mapped[str] = mapped_column(String(32))
    source_ref: Mapped[str] = mapped_column(String(1024))
    declared_version: Mapped[str | None] = mapped_column(String(128), nullable=True)
    resolved_version: Mapped[str | None] = mapped_column(String(128), nullable=True)
    #: Canonical normalized structure (bounded; see
    #: patchfrog.dependencies.registry.MAX_NORMALIZED_CONTRACT_BYTES).
    normalized_contract: Mapped[str | None] = mapped_column(Text, nullable=True)
    summary_json: Mapped[str] = mapped_column(Text, default="{}")
    first_observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    first_observed_commit_sha: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_observed_commit_sha: Mapped[str | None] = mapped_column(String(64), nullable=True)


__all__ = [
    "ExternalContractSnapshotModel",
    "ExternalDependencyModel",
    "ExternalDependencyUsageSiteModel",
]
