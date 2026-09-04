"""Add Test Intelligence Foundation run-level summary columns

Revision ID: 0021_test_intelligence
Revises: 0020_intent_verification
Create Date: 2026-09-05

"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0021_test_intelligence"
down_revision: str | None = "0020_intent_verification"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("review_runs", sa.Column("test_expectation_count", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("review_runs", sa.Column("test_reason_code_counts", sa.Text(), nullable=False, server_default="{}"))
    op.add_column(
        "review_runs", sa.Column("test_gap_candidate_count", sa.Integer(), nullable=False, server_default="0")
    )
    op.add_column(
        "review_runs",
        sa.Column("test_coverage_summary_rendered", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column("review_runs", sa.Column("test_coverage_summary_text", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("review_runs", "test_coverage_summary_text")
    op.drop_column("review_runs", "test_coverage_summary_rendered")
    op.drop_column("review_runs", "test_gap_candidate_count")
    op.drop_column("review_runs", "test_reason_code_counts")
    op.drop_column("review_runs", "test_expectation_count")
