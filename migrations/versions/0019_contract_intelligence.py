"""Add Contract & Blast Radius Intelligence run-level summary columns

Revision ID: 0019_contract_intelligence
Revises: 0018_change_intelligence
Create Date: 2026-09-02

"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0019_contract_intelligence"
down_revision: str | None = "0018_change_intelligence"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("review_runs", sa.Column("contract_delta_count", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("review_runs", sa.Column("contract_kind_counts", sa.Text(), nullable=False, server_default="{}"))
    op.add_column(
        "review_runs",
        sa.Column("potentially_breaking_delta_count", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "review_runs", sa.Column("impacted_consumer_count", sa.Integer(), nullable=False, server_default="0")
    )
    op.add_column(
        "review_runs",
        sa.Column("stale_consumer_candidate_count", sa.Integer(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    op.drop_column("review_runs", "stale_consumer_candidate_count")
    op.drop_column("review_runs", "impacted_consumer_count")
    op.drop_column("review_runs", "potentially_breaking_delta_count")
    op.drop_column("review_runs", "contract_kind_counts")
    op.drop_column("review_runs", "contract_delta_count")
