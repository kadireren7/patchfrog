"""Add Trajectory Intelligence Foundation run-level summary columns

Revision ID: 0024_trajectory_intelligence
Revises: 0023_repository_learnings
Create Date: 2026-09-05

"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0024_trajectory_intelligence"
down_revision: str | None = "0023_repository_learnings"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("review_runs", sa.Column("trajectory_head_count", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("review_runs", sa.Column("trajectory_event_count", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("review_runs", sa.Column("trajectory_signal_count", sa.Integer(), nullable=False, server_default="0"))
    op.add_column(
        "review_runs",
        sa.Column("trajectory_repeated_surface_churn_count", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "review_runs", sa.Column("trajectory_require_critic_count", sa.Integer(), nullable=False, server_default="0")
    )
    op.add_column(
        "review_runs", sa.Column("trajectory_deepen_context_count", sa.Integer(), nullable=False, server_default="0")
    )


def downgrade() -> None:
    op.drop_column("review_runs", "trajectory_deepen_context_count")
    op.drop_column("review_runs", "trajectory_require_critic_count")
    op.drop_column("review_runs", "trajectory_repeated_surface_churn_count")
    op.drop_column("review_runs", "trajectory_signal_count")
    op.drop_column("review_runs", "trajectory_event_count")
    op.drop_column("review_runs", "trajectory_head_count")
