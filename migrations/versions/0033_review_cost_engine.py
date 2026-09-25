"""M4 Ultra-Low-Cost Review Engine: persist risk tier, escalation and context cost facts

Revision ID: 0033_review_cost_engine
Revises: 0032_review_cost_budget
Create Date: 2026-09-24

"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0033_review_cost_engine"
down_revision: str | None = "0032_review_cost_budget"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("review_runs", sa.Column("review_strategy", sa.String(length=32), nullable=True))
    op.add_column("review_runs", sa.Column("risk_tier", sa.String(length=16), nullable=True))
    op.add_column("review_runs", sa.Column("risk_signals", sa.Text(), nullable=False, server_default="[]"))
    op.add_column("review_runs", sa.Column("no_ai_reason", sa.String(length=64), nullable=True))
    op.add_column("review_runs", sa.Column("escalation_reasons", sa.Text(), nullable=False, server_default="[]"))
    op.add_column(
        "review_runs", sa.Column("context_initial_tokens", sa.Integer(), nullable=False, server_default="0")
    )
    op.add_column(
        "review_runs", sa.Column("context_expanded_tokens", sa.Integer(), nullable=False, server_default="0")
    )
    op.add_column(
        "review_runs", sa.Column("context_expansion_reasons", sa.Text(), nullable=False, server_default="[]")
    )
    op.add_column("review_runs", sa.Column("cost_policy_fingerprint", sa.String(length=64), nullable=True))
    op.add_column("review_runs", sa.Column("forced", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.create_index("ix_review_runs_risk_tier", "review_runs", ["risk_tier"])


def downgrade() -> None:
    op.drop_index("ix_review_runs_risk_tier", table_name="review_runs")
    op.drop_column("review_runs", "forced")
    op.drop_column("review_runs", "cost_policy_fingerprint")
    op.drop_column("review_runs", "context_expansion_reasons")
    op.drop_column("review_runs", "context_expanded_tokens")
    op.drop_column("review_runs", "context_initial_tokens")
    op.drop_column("review_runs", "escalation_reasons")
    op.drop_column("review_runs", "no_ai_reason")
    op.drop_column("review_runs", "risk_signals")
    op.drop_column("review_runs", "risk_tier")
    op.drop_column("review_runs", "review_strategy")
