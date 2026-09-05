"""Add Repository Learnings Foundation run-level summary columns

Revision ID: 0023_repository_learnings
Revises: 0022_historical_regression
Create Date: 2026-09-05

"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0023_repository_learnings"
down_revision: str | None = "0022_historical_regression"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "review_runs", sa.Column("repository_learning_active_count", sa.Integer(), nullable=False, server_default="0")
    )
    op.add_column(
        "review_runs",
        sa.Column("repository_learning_application_count", sa.Integer(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    op.drop_column("review_runs", "repository_learning_application_count")
    op.drop_column("review_runs", "repository_learning_active_count")
