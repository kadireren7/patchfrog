"""Add Quality + Cost Guard tier/cost accounting columns

Revision ID: 0016_quality_cost_guard
Revises: 0015_adaptive_multihop_context
Create Date: 2026-08-30

"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0016_quality_cost_guard"
down_revision: str | None = "0015_adaptive_multihop_context"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("review_candidates", sa.Column("effort_tier", sa.String(length=16), nullable=True))
    op.add_column(
        "review_candidates", sa.Column("effort_reasons", sa.Text(), nullable=False, server_default="[]")
    )
    op.add_column(
        "review_candidates", sa.Column("escalated", sa.Boolean(), nullable=False, server_default=sa.false())
    )
    op.add_column(
        "review_candidates", sa.Column("escalation_reason", sa.String(length=32), nullable=True)
    )

    op.add_column("critic_verdicts", sa.Column("thinking_tokens", sa.Integer(), nullable=False, server_default="0"))

    op.add_column(
        "review_runs", sa.Column("correctness_thinking_tokens", sa.Integer(), nullable=False, server_default="0")
    )
    op.add_column(
        "review_runs", sa.Column("security_thinking_tokens", sa.Integer(), nullable=False, server_default="0")
    )
    op.add_column(
        "review_runs", sa.Column("reviewer_thinking_tokens", sa.Integer(), nullable=False, server_default="0")
    )
    op.add_column(
        "review_runs", sa.Column("critic_thinking_tokens", sa.Integer(), nullable=False, server_default="0")
    )
    op.add_column(
        "review_runs", sa.Column("candidates_by_tier", sa.Text(), nullable=False, server_default="{}")
    )
    op.add_column(
        "review_runs", sa.Column("candidates_escalated", sa.Integer(), nullable=False, server_default="0")
    )
    op.add_column("review_runs", sa.Column("critic_calls", sa.Integer(), nullable=False, server_default="0"))
    op.add_column(
        "review_runs", sa.Column("retries_consumed", sa.Integer(), nullable=False, server_default="0")
    )


def downgrade() -> None:
    op.drop_column("review_runs", "retries_consumed")
    op.drop_column("review_runs", "critic_calls")
    op.drop_column("review_runs", "candidates_escalated")
    op.drop_column("review_runs", "candidates_by_tier")
    op.drop_column("review_runs", "critic_thinking_tokens")
    op.drop_column("review_runs", "reviewer_thinking_tokens")
    op.drop_column("review_runs", "security_thinking_tokens")
    op.drop_column("review_runs", "correctness_thinking_tokens")
    op.drop_column("critic_verdicts", "thinking_tokens")
    op.drop_column("review_candidates", "escalation_reason")
    op.drop_column("review_candidates", "escalated")
    op.drop_column("review_candidates", "effort_reasons")
    op.drop_column("review_candidates", "effort_tier")
