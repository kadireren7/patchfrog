"""M8 migration verification runs

Revision ID: 0036_migration_verification
Revises: 0035_upstream_change_migration
Create Date: 2026-09-26

"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0036_migration_verification"
down_revision: str | None = "0035_upstream_change_migration"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "migration_verification_runs",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("patch_id", sa.Uuid(), sa.ForeignKey("migration_patches.id", ondelete="CASCADE"), nullable=False),
        sa.Column("change_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("patch_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("verification_plan_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("engine_version", sa.Integer(), nullable=False),
        sa.Column("repository_head_sha", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("outcome", sa.String(length=24), nullable=False),
        sa.Column("evidence_strength", sa.String(length=16), nullable=False),
        sa.Column("residual_risk", sa.String(length=16), nullable=False),
        sa.Column("bundle_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_verified_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "patch_id", "verification_plan_fingerprint", name="uq_migration_verification_runs_identity"
        ),
    )
    op.create_index("ix_migration_verification_runs_patch", "migration_verification_runs", ["patch_id"])


def downgrade() -> None:
    op.drop_index("ix_migration_verification_runs_patch", table_name="migration_verification_runs")
    op.drop_table("migration_verification_runs")
