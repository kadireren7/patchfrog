"""Add Cross-PR Intelligence Foundation run-level summary columns

Revision ID: 0025_cross_pr_intelligence
Revises: 0024_trajectory_intelligence
Create Date: 2026-09-06

"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0025_cross_pr_intelligence"
down_revision: str | None = "0024_trajectory_intelligence"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("review_runs", sa.Column("cross_pr_peer_count", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("review_runs", sa.Column("cross_pr_overlap_count", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("review_runs", sa.Column("cross_pr_signal_count", sa.Integer(), nullable=False, server_default="0"))
    op.add_column(
        "review_runs",
        sa.Column("cross_pr_same_changed_symbol_count", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "review_runs", sa.Column("cross_pr_require_critic_count", sa.Integer(), nullable=False, server_default="0")
    )
    op.add_column(
        "review_runs", sa.Column("cross_pr_deepen_context_count", sa.Integer(), nullable=False, server_default="0")
    )


def downgrade() -> None:
    op.drop_column("review_runs", "cross_pr_deepen_context_count")
    op.drop_column("review_runs", "cross_pr_require_critic_count")
    op.drop_column("review_runs", "cross_pr_same_changed_symbol_count")
    op.drop_column("review_runs", "cross_pr_signal_count")
    op.drop_column("review_runs", "cross_pr_overlap_count")
    op.drop_column("review_runs", "cross_pr_peer_count")
