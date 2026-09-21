"""Add first-class review budget and cost telemetry

Revision ID: 0032_review_cost_budget
Revises: 0031_critic_rejection_category
Create Date: 2026-09-21

"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0032_review_cost_budget"
down_revision: str | None = "0031_critic_rejection_category"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("review_runs", sa.Column("provider_calls", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("review_runs", sa.Column("retry_attempts", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("review_runs", sa.Column("budget_input_tokens", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("review_runs", sa.Column("budget_output_tokens", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("review_runs", sa.Column("estimated_cost_usd", sa.Float(), nullable=False, server_default="0"))
    op.add_column("review_runs", sa.Column("budget_elapsed_seconds", sa.Float(), nullable=False, server_default="0"))
    op.add_column("review_runs", sa.Column("budget_termination_reason", sa.String(length=32), nullable=True))
    op.add_column(
        "review_runs", sa.Column("provider_cost_breakdown", sa.Text(), nullable=False, server_default="[]")
    )


def downgrade() -> None:
    op.drop_column("review_runs", "provider_cost_breakdown")
    op.drop_column("review_runs", "budget_termination_reason")
    op.drop_column("review_runs", "budget_elapsed_seconds")
    op.drop_column("review_runs", "estimated_cost_usd")
    op.drop_column("review_runs", "budget_output_tokens")
    op.drop_column("review_runs", "budget_input_tokens")
    op.drop_column("review_runs", "retry_attempts")
    op.drop_column("review_runs", "provider_calls")
