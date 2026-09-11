"""Add Repository Learning Records table (Milestone Y)

Revision ID: 0030_repository_learning_records
Revises: 0029_fix_attempts
Create Date: 2026-09-11

"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0030_repository_learning_records"
down_revision: str | None = "0029_fix_attempts"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "repository_learning_records",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("repository_id", sa.Uuid(), nullable=False),
        sa.Column("learning_type", sa.String(length=32), nullable=False),
        sa.Column("surface_file_path", sa.String(length=1024), nullable=False),
        sa.Column("surface_qualified_name", sa.String(length=512), nullable=False),
        sa.Column("surface_category", sa.String(length=32), nullable=False),
        sa.Column("maturity", sa.String(length=16), nullable=False),
        sa.Column("support_count", sa.Integer(), nullable=False),
        sa.Column("evidence_json", sa.Text(), nullable=False),
        sa.Column("first_observed_at", sa.String(length=64), nullable=False),
        sa.Column("last_observed_at", sa.String(length=64), nullable=False),
        sa.Column("retired_reason", sa.String(length=255), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            onupdate=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "repository_id",
            "learning_type",
            "surface_file_path",
            "surface_qualified_name",
            "surface_category",
            name="uq_repository_learning_record_surface",
        ),
    )
    op.create_index(
        "ix_repository_learning_records_repository_id", "repository_learning_records", ["repository_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_repository_learning_records_repository_id", table_name="repository_learning_records")
    op.drop_table("repository_learning_records")
