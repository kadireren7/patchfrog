"""Add agent_role provenance and per-role token usage for Agent Orchestration v1

Revision ID: 0014_agent_orchestration
Revises: 0013_public_beta_readiness
Create Date: 2026-08-28

"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0014_agent_orchestration"
down_revision: str | None = "0013_public_beta_readiness"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("ai_finding_proposals", sa.Column("agent_role", sa.String(length=32), nullable=True))
    op.add_column("ai_findings", sa.Column("agent_role", sa.String(length=32), nullable=True))
    op.add_column(
        "review_runs", sa.Column("correctness_input_tokens", sa.Integer(), nullable=False, server_default="0")
    )
    op.add_column(
        "review_runs", sa.Column("correctness_output_tokens", sa.Integer(), nullable=False, server_default="0")
    )
    op.add_column(
        "review_runs", sa.Column("security_input_tokens", sa.Integer(), nullable=False, server_default="0")
    )
    op.add_column(
        "review_runs", sa.Column("security_output_tokens", sa.Integer(), nullable=False, server_default="0")
    )


def downgrade() -> None:
    op.drop_column("review_runs", "security_output_tokens")
    op.drop_column("review_runs", "security_input_tokens")
    op.drop_column("review_runs", "correctness_output_tokens")
    op.drop_column("review_runs", "correctness_input_tokens")
    op.drop_column("ai_findings", "agent_role")
    op.drop_column("ai_finding_proposals", "agent_role")
