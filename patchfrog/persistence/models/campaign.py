"""Compatibility campaign persistence (M10).

Two tables. ``compatibility_campaigns`` holds one row per
(workspace, upstream change, campaign engine version) -- the identity the
spec requires so that re-detecting the same change reuses one campaign.
``campaign_repository_records`` holds one row per repository per campaign.

Engine truth only: ``workspace_key`` is an opaque string supplied by the
caller (a hosted workspace id, an installation id, an org login). The
engine never interprets it. Every column is a bounded, controlled value --
no source text, no logs, no secrets.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import Uuid

from patchfrog.persistence.models.base import Base


class CompatibilityCampaignModel(Base):
    __tablename__ = "compatibility_campaigns"
    __table_args__ = (
        UniqueConstraint("identity_key", name="uq_compatibility_campaigns_identity"),
        Index("ix_compatibility_campaigns_workspace", "workspace_key", "state"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    identity_key: Mapped[str] = mapped_column(String(64))
    workspace_key: Mapped[str] = mapped_column(String(255))
    change_fingerprint: Mapped[str] = mapped_column(String(64))
    engine_version: Mapped[int] = mapped_column(Integer)
    dependency_label: Mapped[str] = mapped_column(String(255))
    provider_key: Mapped[str | None] = mapped_column(String(255), nullable=True)
    origin: Mapped[str] = mapped_column(String(16))
    producer_repository: Mapped[str | None] = mapped_column(String(512), nullable=True)
    old_version: Mapped[str | None] = mapped_column(String(128), nullable=True)
    new_version: Mapped[str | None] = mapped_column(String(128), nullable=True)
    compatibility: Mapped[str | None] = mapped_column(String(32), nullable=True)
    #: ``CampaignState`` value.
    state: Mapped[str] = mapped_column(String(24))
    version: Mapped[int] = mapped_column(Integer, default=1)
    #: JSON: org blast radius lists/reasons (derived, kept for fast reads).
    blast_json: Mapped[str] = mapped_column(Text)
    #: JSON list of bounded strings.
    unresolved_risks_json: Mapped[str] = mapped_column(Text, default="[]")
    discovered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class CampaignRepositoryRecordModel(Base):
    __tablename__ = "campaign_repository_records"
    __table_args__ = (
        UniqueConstraint("campaign_id", "repository", name="uq_campaign_repository_records_campaign_repo"),
        Index("ix_campaign_repository_records_repository", "repository", "state"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    campaign_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("compatibility_campaigns.id", ondelete="CASCADE"), index=True
    )
    repository: Mapped[str] = mapped_column(String(512))
    state: Mapped[str] = mapped_column(String(24))
    org_class: Mapped[str] = mapped_column(String(16))
    freshness: Mapped[str] = mapped_column(String(16))
    unknown_reason: Mapped[str | None] = mapped_column(String(32), nullable=True)
    impact_status: Mapped[str | None] = mapped_column(String(16), nullable=True)
    direct_consumers: Mapped[int] = mapped_column(Integer, default=0)
    transitive_consumers: Mapped[int] = mapped_column(Integer, default=0)
    potential_consumers: Mapped[int] = mapped_column(Integer, default=0)
    severity: Mapped[str] = mapped_column(String(32), default="none")
    migration_status: Mapped[str | None] = mapped_column(String(24), nullable=True)
    migration_strategy_available: Mapped[bool] = mapped_column(Boolean, default=False)
    verification_feasibility: Mapped[str] = mapped_column(String(24), default="not_applicable")
    verification_outcome: Mapped[str | None] = mapped_column(String(24), nullable=True)
    publication_readiness: Mapped[str | None] = mapped_column(String(32), nullable=True)
    base_commit_sha: Mapped[str | None] = mapped_column(String(64), nullable=True)
    plan_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    patch_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    bundle_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    pr_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    pr_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    pr_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    residual_risk: Mapped[str | None] = mapped_column(String(16), nullable=True)
    ever_affected: Mapped[bool] = mapped_column(Boolean, default=False)
    reasons_json: Mapped[str] = mapped_column(Text, default="[]")
    human_actions_json: Mapped[str] = mapped_column(Text, default="[]")
    error: Mapped[str | None] = mapped_column(String(512), nullable=True)
    evaluated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


__all__ = ["CampaignRepositoryRecordModel", "CompatibilityCampaignModel"]
