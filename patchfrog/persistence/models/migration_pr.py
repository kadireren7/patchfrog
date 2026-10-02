"""Migration pull-request publication persistence (M9.1/M9.6).

One bounded table, keyed by the migration's stable identity (repository +
upstream change fingerprint + engine version -- see
:meth:`patchfrog.migration_pr.domain.MigrationPRLinkage.identity_key`),
never by patch/evidence fingerprint: the whole point is that a
*regenerated* patch for the same upstream change reconciles the same row
(and the same GitHub PR) rather than creating a duplicate (M9.6).
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import Uuid

from patchfrog.persistence.models.base import Base


class MigrationPRModel(Base):
    __tablename__ = "migration_pull_requests"
    __table_args__ = (
        UniqueConstraint("identity_key", name="uq_migration_pull_requests_identity"),
        Index("ix_migration_pull_requests_repository", "repository"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    identity_key: Mapped[str] = mapped_column(String(64))
    repository: Mapped[str] = mapped_column(String(512))
    change_fingerprint: Mapped[str] = mapped_column(String(64))
    engine_version: Mapped[int] = mapped_column(Integer)
    branch_name: Mapped[str] = mapped_column(String(255))
    patch_fingerprint: Mapped[str] = mapped_column(String(64))
    verification_plan_fingerprint: Mapped[str] = mapped_column(String(64))
    bundle_fingerprint: Mapped[str] = mapped_column(String(64))
    base_commit_sha: Mapped[str] = mapped_column(String(64))
    #: MigrationPREligibility value.
    eligibility: Mapped[str] = mapped_column(String(32))
    #: MigrationPRStatus value.
    status: Mapped[str] = mapped_column(String(32))
    pr_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    pr_html_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_reconciled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


__all__ = ["MigrationPRModel"]
