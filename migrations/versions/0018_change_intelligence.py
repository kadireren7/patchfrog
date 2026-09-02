"""Add Change Intelligence Foundation run-level summary columns

Revision ID: 0018_change_intelligence
Revises: 0017_telemetry_intelligence
Create Date: 2026-09-02

"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0018_change_intelligence"
down_revision: str | None = "0017_telemetry_intelligence"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("review_runs", sa.Column("change_unit_count", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("review_runs", sa.Column("change_kind_counts", sa.Text(), nullable=False, server_default="{}"))
    op.add_column(
        "review_runs", sa.Column("affected_surface_count", sa.Integer(), nullable=False, server_default="0")
    )
    op.add_column(
        "review_runs", sa.Column("expected_companion_count", sa.Integer(), nullable=False, server_default="0")
    )
    op.add_column(
        "review_runs",
        sa.Column("missing_companion_candidate_count", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "review_runs", sa.Column("change_map_rendered", sa.Boolean(), nullable=False, server_default=sa.false())
    )
    op.add_column(
        "review_runs", sa.Column("change_map_node_count", sa.Integer(), nullable=False, server_default="0")
    )
    op.add_column("review_runs", sa.Column("change_story", sa.Text(), nullable=True))
    op.add_column("review_runs", sa.Column("change_map_text", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("review_runs", "change_map_text")
    op.drop_column("review_runs", "change_story")
    op.drop_column("review_runs", "change_map_node_count")
    op.drop_column("review_runs", "change_map_rendered")
    op.drop_column("review_runs", "missing_companion_candidate_count")
    op.drop_column("review_runs", "expected_companion_count")
    op.drop_column("review_runs", "affected_surface_count")
    op.drop_column("review_runs", "change_kind_counts")
    op.drop_column("review_runs", "change_unit_count")
