"""Upstream change history (M6.9) + migration plans/patches (M7).

Five bounded tables. Full normalized contracts are **not** stored here:
M5's ``external_contract_snapshots`` own contract storage; an event keeps
only both contract fingerprints/versions plus its diff items.

- ``external_change_events`` -- one row per distinct upstream change
  (unique ``fingerprint``; re-ingestion only moves ``last_observed_at``).
  Global, not per repository: the same upstream change affects many.
- ``external_change_diff_items`` -- the deterministic diff of an event
  (immutable for a given fingerprint). Cascades from the event.
- ``external_change_impacts`` -- what one event means for one repository
  (+ matched dependency) at one analyzed commit. Cascades from the event
  and the repository; the dependency link is ``SET NULL`` so deleting a
  dependency row never erases the historical impact record.
- ``migration_plans`` -- one plan per (event, repository, exact
  repository state); cascades from both.
- ``migration_patches`` -- generated patches of a plan with their linkage
  and safety-gate results (unique per plan + patch fingerprint); cascade
  from the plan.

No raw source file, spec text, release notes or secret value is stored:
diff items carry bounded canonical fragments; a patch is stored only
because it *is* the product output a later M9 pull request is built from,
and only after the M7.5 gates (which refuse secret-store files) passed.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import Boolean, Uuid

from patchfrog.persistence.models.base import Base


class ExternalChangeEventModel(Base):
    __tablename__ = "external_change_events"
    __table_args__ = (
        UniqueConstraint("fingerprint", name="uq_external_change_events_fingerprint"),
        Index("ix_external_change_events_package", "ecosystem", "package_name"),
        Index("ix_external_change_events_provider", "provider_key"),
        Index("ix_external_change_events_dependency_key", "dependency_key"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    fingerprint: Mapped[str] = mapped_column(String(64))
    engine_version: Mapped[int] = mapped_column(Integer)
    provider_key: Mapped[str | None] = mapped_column(String(255), nullable=True)
    ecosystem: Mapped[str | None] = mapped_column(String(16), nullable=True)
    package_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    dependency_key: Mapped[str | None] = mapped_column(String(512), nullable=True)
    target_label: Mapped[str] = mapped_column(String(255))
    source_type: Mapped[str] = mapped_column(String(32))
    change_kind: Mapped[str] = mapped_column(String(32))
    source_ref: Mapped[str] = mapped_column(String(1024))
    old_version: Mapped[str | None] = mapped_column(String(128), nullable=True)
    new_version: Mapped[str | None] = mapped_column(String(128), nullable=True)
    old_contract_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    new_contract_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    contract_format: Mapped[str | None] = mapped_column(String(16), nullable=True)
    risk: Mapped[str] = mapped_column(String(24))
    compatibility: Mapped[str] = mapped_column(String(24))
    reasons_json: Mapped[str] = mapped_column(Text, default="[]")
    counts_json: Mapped[str] = mapped_column(Text, default="{}")
    diff_item_count: Mapped[int] = mapped_column(Integer, default=0)
    truncated: Mapped[bool] = mapped_column(Boolean, default=False)
    hints_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    hints_provenance: Mapped[str | None] = mapped_column(String(200), nullable=True)
    release_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    notes_json: Mapped[str] = mapped_column(Text, default="[]")
    first_observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ExternalChangeDiffItemModel(Base):
    __tablename__ = "external_change_diff_items"
    __table_args__ = (
        UniqueConstraint("event_id", "item_key", name="uq_external_change_diff_items_identity"),
        Index("ix_external_change_diff_items_event", "event_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    event_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("external_change_events.id", ondelete="CASCADE")
    )
    item_key: Mapped[str] = mapped_column(String(16))
    kind: Mapped[str] = mapped_column(String(48))
    location: Mapped[str] = mapped_column(String(1024))
    subject_json: Mapped[str] = mapped_column(Text)
    compatibility: Mapped[str] = mapped_column(String(24))
    old_repr: Mapped[str | None] = mapped_column(Text, nullable=True)
    new_repr: Mapped[str | None] = mapped_column(Text, nullable=True)
    explanation: Mapped[str] = mapped_column(Text)
    evidence_json: Mapped[str] = mapped_column(Text, default="[]")
    replacement: Mapped[str | None] = mapped_column(String(512), nullable=True)


class ExternalChangeImpactModel(Base):
    __tablename__ = "external_change_impacts"
    __table_args__ = (
        UniqueConstraint(
            "event_id", "repository_id", "dependency_key", "analyzed_commit_sha",
            name="uq_external_change_impacts_identity",
        ),
        Index("ix_external_change_impacts_repository", "repository_id", "status"),
        Index("ix_external_change_impacts_event", "event_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    event_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("external_change_events.id", ondelete="CASCADE")
    )
    repository_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("repositories.id", ondelete="CASCADE"))
    dependency_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("external_dependencies.id", ondelete="SET NULL"), nullable=True
    )
    #: ``""`` when the repository does not use the dependency at all --
    #: never NULL, so the unique constraint really de-duplicates.
    dependency_key: Mapped[str] = mapped_column(String(512), default="")
    #: ``""`` for a non-git local checkout.
    analyzed_commit_sha: Mapped[str] = mapped_column(String(64), default="")
    #: ``affected`` | ``uncertain`` | ``unaffected``.
    status: Mapped[str] = mapped_column(String(16))
    reason: Mapped[str] = mapped_column(String(255))
    direct_count: Mapped[int] = mapped_column(Integer, default=0)
    transitive_count: Mapped[int] = mapped_column(Integer, default=0)
    potential_count: Mapped[int] = mapped_column(Integer, default=0)
    related_test_count: Mapped[int] = mapped_column(Integer, default=0)
    consumers_json: Mapped[str] = mapped_column(Text, default="[]")
    blast_radius_json: Mapped[str] = mapped_column(Text, default="{}")
    first_analyzed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_analyzed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class MigrationPlanModel(Base):
    __tablename__ = "migration_plans"
    __table_args__ = (
        UniqueConstraint("plan_fingerprint", name="uq_migration_plans_fingerprint"),
        Index("ix_migration_plans_event_repository", "event_id", "repository_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    event_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("external_change_events.id", ondelete="CASCADE")
    )
    repository_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("repositories.id", ondelete="CASCADE"))
    plan_fingerprint: Mapped[str] = mapped_column(String(64))
    engine_version: Mapped[int] = mapped_column(Integer)
    base_commit_sha: Mapped[str] = mapped_column(String(64), default="")
    #: sha256 over the planned target files' content -- identifies the
    #: exact repository state even without git.
    base_content_fingerprint: Mapped[str] = mapped_column(String(64))
    dependency_keys_json: Mapped[str] = mapped_column(Text, default="[]")
    #: MigrationStatus value.
    status: Mapped[str] = mapped_column(String(24))
    residual_risk: Mapped[str] = mapped_column(String(16))
    step_count: Mapped[int] = mapped_column(Integer, default=0)
    auto_step_count: Mapped[int] = mapped_column(Integer, default=0)
    human_step_count: Mapped[int] = mapped_column(Integer, default=0)
    steps_json: Mapped[str] = mapped_column(Text, default="[]")
    unresolved_json: Mapped[str] = mapped_column(Text, default="[]")
    evidence_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class MigrationPatchModel(Base):
    __tablename__ = "migration_patches"
    __table_args__ = (
        UniqueConstraint("plan_id", "patch_fingerprint", name="uq_migration_patches_identity"),
        Index("ix_migration_patches_plan", "plan_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    plan_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("migration_plans.id", ondelete="CASCADE"))
    patch_fingerprint: Mapped[str] = mapped_column(String(64))
    #: ``deterministic`` | ``model_assisted``.
    origin: Mapped[str] = mapped_column(String(24))
    #: ``candidate`` (all safety gates passed) | ``rejected``.
    status: Mapped[str] = mapped_column(String(24))
    #: The unified diff (bounded); ``None`` when rejected or oversized.
    unified_diff: Mapped[str | None] = mapped_column(Text, nullable=True)
    modified_files_json: Mapped[str] = mapped_column(Text, default="[]")
    linkage_json: Mapped[str] = mapped_column(Text, default="{}")
    safety_json: Mapped[str] = mapped_column(Text, default="[]")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


__all__ = [
    "ExternalChangeDiffItemModel",
    "ExternalChangeEventModel",
    "ExternalChangeImpactModel",
    "MigrationPatchModel",
    "MigrationPlanModel",
]
