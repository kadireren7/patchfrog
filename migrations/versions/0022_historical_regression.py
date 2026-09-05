"""Add Historical Regression Memory Foundation run-level summary columns

Revision ID: 0022_historical_regression
Revises: 0021_test_intelligence
Create Date: 2026-09-05

"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0022_historical_regression"
down_revision: str | None = "0021_test_intelligence"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "review_runs", sa.Column("historical_trusted_record_count", sa.Integer(), nullable=False, server_default="0")
    )
    op.add_column(
        "review_runs", sa.Column("historical_match_kind_counts", sa.Text(), nullable=False, server_default="{}")
    )
    op.add_column(
        "review_runs",
        sa.Column("historical_regression_candidate_count", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "review_runs",
        sa.Column("historical_summary_rendered", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column("review_runs", sa.Column("historical_summary_text", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("review_runs", "historical_summary_text")
    op.drop_column("review_runs", "historical_summary_rendered")
    op.drop_column("review_runs", "historical_regression_candidate_count")
    op.drop_column("review_runs", "historical_match_kind_counts")
    op.drop_column("review_runs", "historical_trusted_record_count")
