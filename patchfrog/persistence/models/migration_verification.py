"""Migration Verification run persistence (M8.9's evidence bundle).

One bounded table. Mirrors :mod:`patchfrog.persistence.models.upstream`'s
``migration_patches`` pattern exactly: one row per (patch, verification
plan fingerprint) -- a re-run against the identical patch and requirement
set is idempotent (only ``last_verified_at`` moves); a different patch
fingerprint (the migration changed) or a different verification-plan
fingerprint (the M8 engine's own requirement/step derivation changed) is
never matched against a stale row -- old evidence is never silently
reused for a new patch.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import Uuid

from patchfrog.persistence.models.base import Base


class MigrationVerificationRunModel(Base):
    __tablename__ = "migration_verification_runs"
    __table_args__ = (
        UniqueConstraint(
            "patch_id", "verification_plan_fingerprint", name="uq_migration_verification_runs_identity"
        ),
        Index("ix_migration_verification_runs_patch", "patch_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    patch_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("migration_patches.id", ondelete="CASCADE"))
    change_fingerprint: Mapped[str] = mapped_column(String(64))
    patch_fingerprint: Mapped[str] = mapped_column(String(64))
    verification_plan_fingerprint: Mapped[str] = mapped_column(String(64))
    engine_version: Mapped[int] = mapped_column(Integer)
    repository_head_sha: Mapped[str] = mapped_column(String(64), default="")
    #: VerificationOutcome value.
    outcome: Mapped[str] = mapped_column(String(24))
    evidence_strength: Mapped[str] = mapped_column(String(16))
    residual_risk: Mapped[str] = mapped_column(String(16))
    bundle_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_verified_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


__all__ = ["MigrationVerificationRunModel"]
