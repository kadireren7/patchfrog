"""M9 migration pull request publication

Revision ID: 0037_migration_pull_requests
Revises: 0036_migration_verification
Create Date: 2026-09-26

"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0037_migration_pull_requests"
down_revision: str | None = "0036_migration_verification"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "migration_pull_requests",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("identity_key", sa.String(length=64), nullable=False),
        sa.Column("repository", sa.String(length=512), nullable=False),
        sa.Column("change_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("engine_version", sa.Integer(), nullable=False),
        sa.Column("branch_name", sa.String(length=255), nullable=False),
        sa.Column("patch_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("verification_plan_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("bundle_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("base_commit_sha", sa.String(length=64), nullable=False),
        sa.Column("eligibility", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("pr_number", sa.Integer(), nullable=True),
        sa.Column("pr_html_url", sa.String(length=1024), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_reconciled_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("identity_key", name="uq_migration_pull_requests_identity"),
    )
    op.create_index("ix_migration_pull_requests_repository", "migration_pull_requests", ["repository"])


def downgrade() -> None:
    op.drop_index("ix_migration_pull_requests_repository", table_name="migration_pull_requests")
    op.drop_table("migration_pull_requests")
