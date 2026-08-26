"""Add installations table and repositories.is_selected

Revision ID: 0013_public_beta_readiness
Revises: 0012_feedback_loop
Create Date: 2026-08-23

"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0013_public_beta_readiness"
down_revision: str | None = "0012_feedback_loop"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "repositories",
        sa.Column("is_selected", sa.Boolean(), nullable=False, server_default=sa.true()),
    )

    op.create_table(
        "installations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("github_installation_id", sa.BigInteger(), nullable=False),
        sa.Column("account_login", sa.String(length=255), nullable=False),
        sa.Column("account_type", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="active"),
        sa.Column("beta_state", sa.String(length=16), nullable=False, server_default="active"),
        sa.Column("publication_allowed", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("daily_review_limit", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_installations_github_installation_id",
        "installations",
        ["github_installation_id"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_table("installations")
    op.drop_column("repositories", "is_selected")
