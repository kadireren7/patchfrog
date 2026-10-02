"""M10 compatibility campaigns

Revision ID: 0038_compatibility_campaigns
Revises: 0037_migration_pull_requests
Create Date: 2026-10-02

"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0038_compatibility_campaigns"
down_revision: str | None = "0037_migration_pull_requests"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "compatibility_campaigns",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("identity_key", sa.String(length=64), nullable=False),
        sa.Column("workspace_key", sa.String(length=255), nullable=False),
        sa.Column("change_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("engine_version", sa.Integer(), nullable=False),
        sa.Column("dependency_label", sa.String(length=255), nullable=False),
        sa.Column("provider_key", sa.String(length=255), nullable=True),
        sa.Column("origin", sa.String(length=16), nullable=False),
        sa.Column("producer_repository", sa.String(length=512), nullable=True),
        sa.Column("old_version", sa.String(length=128), nullable=True),
        sa.Column("new_version", sa.String(length=128), nullable=True),
        sa.Column("compatibility", sa.String(length=32), nullable=True),
        sa.Column("state", sa.String(length=24), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("blast_json", sa.Text(), nullable=False),
        sa.Column("unresolved_risks_json", sa.Text(), nullable=False),
        sa.Column("discovered_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("identity_key", name="uq_compatibility_campaigns_identity"),
    )
    op.create_index("ix_compatibility_campaigns_workspace", "compatibility_campaigns", ["workspace_key", "state"])
    op.create_table(
        "campaign_repository_records",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "campaign_id", sa.Uuid(), sa.ForeignKey("compatibility_campaigns.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("repository", sa.String(length=512), nullable=False),
        sa.Column("state", sa.String(length=24), nullable=False),
        sa.Column("org_class", sa.String(length=16), nullable=False),
        sa.Column("freshness", sa.String(length=16), nullable=False),
        sa.Column("unknown_reason", sa.String(length=32), nullable=True),
        sa.Column("impact_status", sa.String(length=16), nullable=True),
        sa.Column("direct_consumers", sa.Integer(), nullable=False),
        sa.Column("transitive_consumers", sa.Integer(), nullable=False),
        sa.Column("potential_consumers", sa.Integer(), nullable=False),
        sa.Column("severity", sa.String(length=32), nullable=False),
        sa.Column("migration_status", sa.String(length=24), nullable=True),
        sa.Column("migration_strategy_available", sa.Boolean(), nullable=False),
        sa.Column("verification_feasibility", sa.String(length=24), nullable=False),
        sa.Column("verification_outcome", sa.String(length=24), nullable=True),
        sa.Column("publication_readiness", sa.String(length=32), nullable=True),
        sa.Column("base_commit_sha", sa.String(length=64), nullable=True),
        sa.Column("plan_fingerprint", sa.String(length=64), nullable=True),
        sa.Column("patch_fingerprint", sa.String(length=64), nullable=True),
        sa.Column("bundle_fingerprint", sa.String(length=64), nullable=True),
        sa.Column("pr_status", sa.String(length=32), nullable=True),
        sa.Column("pr_number", sa.Integer(), nullable=True),
        sa.Column("pr_url", sa.String(length=1024), nullable=True),
        sa.Column("residual_risk", sa.String(length=16), nullable=True),
        sa.Column("ever_affected", sa.Boolean(), nullable=False),
        sa.Column("reasons_json", sa.Text(), nullable=False),
        sa.Column("human_actions_json", sa.Text(), nullable=False),
        sa.Column("error", sa.String(length=512), nullable=True),
        sa.Column("evaluated_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("campaign_id", "repository", name="uq_campaign_repository_records_campaign_repo"),
    )
    op.create_index("ix_campaign_repository_records_campaign_id", "campaign_repository_records", ["campaign_id"])
    op.create_index(
        "ix_campaign_repository_records_repository", "campaign_repository_records", ["repository", "state"]
    )


def downgrade() -> None:
    op.drop_index("ix_campaign_repository_records_repository", table_name="campaign_repository_records")
    op.drop_index("ix_campaign_repository_records_campaign_id", table_name="campaign_repository_records")
    op.drop_table("campaign_repository_records")
    op.drop_index("ix_compatibility_campaigns_workspace", table_name="compatibility_campaigns")
    op.drop_table("compatibility_campaigns")
