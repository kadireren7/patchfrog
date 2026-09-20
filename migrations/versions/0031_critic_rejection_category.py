"""Add nullable rejection_category column to critic_verdicts

Revision ID: 0031_critic_rejection_category
Revises: 0030_repository_learning_records
Create Date: 2026-09-21

"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0031_critic_rejection_category"
down_revision: str | None = "0030_repository_learning_records"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "critic_verdicts", sa.Column("rejection_category", sa.String(length=32), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("critic_verdicts", "rejection_category")
