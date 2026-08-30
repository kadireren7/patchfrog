"""Add adaptive multi-hop context provenance to context_bundles

Revision ID: 0015_adaptive_multihop_context
Revises: 0014_agent_orchestration
Create Date: 2026-08-30

"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0015_adaptive_multihop_context"
down_revision: str | None = "0014_agent_orchestration"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "context_bundles",
        sa.Column("adaptive_expansion_attempted", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        "context_bundles",
        sa.Column("adaptive_expansion_occurred", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        "context_bundles",
        sa.Column("adaptive_expansion_reasons", sa.Text(), nullable=False, server_default="[]"),
    )
    op.add_column(
        "context_bundles", sa.Column("adaptive_expansion_direction", sa.String(length=16), nullable=True)
    )
    op.add_column(
        "context_bundles", sa.Column("adaptive_requested_max_depth", sa.Integer(), nullable=True)
    )
    op.add_column(
        "context_bundles", sa.Column("adaptive_effective_max_depth", sa.Integer(), nullable=True)
    )
    op.add_column(
        "context_bundles", sa.Column("depth_2_candidate_count", sa.Integer(), nullable=False, server_default="0")
    )
    op.add_column(
        "context_bundles", sa.Column("depth_2_selected_count", sa.Integer(), nullable=False, server_default="0")
    )
    op.add_column(
        "context_bundles", sa.Column("depth_2_tokens", sa.Integer(), nullable=False, server_default="0")
    )


def downgrade() -> None:
    op.drop_column("context_bundles", "depth_2_tokens")
    op.drop_column("context_bundles", "depth_2_selected_count")
    op.drop_column("context_bundles", "depth_2_candidate_count")
    op.drop_column("context_bundles", "adaptive_effective_max_depth")
    op.drop_column("context_bundles", "adaptive_requested_max_depth")
    op.drop_column("context_bundles", "adaptive_expansion_direction")
    op.drop_column("context_bundles", "adaptive_expansion_reasons")
    op.drop_column("context_bundles", "adaptive_expansion_occurred")
    op.drop_column("context_bundles", "adaptive_expansion_attempted")
