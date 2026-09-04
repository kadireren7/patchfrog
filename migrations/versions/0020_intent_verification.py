"""Add Intent Verification Foundation run-level summary columns

Revision ID: 0020_intent_verification
Revises: 0019_contract_intelligence
Create Date: 2026-09-04

"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0020_intent_verification"
down_revision: str | None = "0019_contract_intelligence"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("review_runs", sa.Column("intent_claim_count", sa.Integer(), nullable=False, server_default="0"))
    op.add_column(
        "review_runs", sa.Column("intent_source_kind_counts", sa.Text(), nullable=False, server_default="{}")
    )
    op.add_column(
        "review_runs", sa.Column("mapped_intent_claim_count", sa.Integer(), nullable=False, server_default="0")
    )
    op.add_column(
        "review_runs", sa.Column("intent_gap_candidate_count", sa.Integer(), nullable=False, server_default="0")
    )
    op.add_column(
        "review_runs",
        sa.Column("intent_coverage_summary_rendered", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column("review_runs", sa.Column("intent_coverage_summary_text", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("review_runs", "intent_coverage_summary_text")
    op.drop_column("review_runs", "intent_coverage_summary_rendered")
    op.drop_column("review_runs", "intent_gap_candidate_count")
    op.drop_column("review_runs", "mapped_intent_claim_count")
    op.drop_column("review_runs", "intent_source_kind_counts")
    op.drop_column("review_runs", "intent_claim_count")
