"""Add composite index serving Cross-PR Intelligence's bounded peer
-discovery query

Revision ID: 0026_cross_pr_peer_index
Revises: 0025_cross_pr_intelligence
Create Date: 2026-09-06

"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0026_cross_pr_peer_index"
down_revision: str | None = "0025_cross_pr_intelligence"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "ix_pull_requests_repo_state_updated",
        "pull_requests",
        ["repository_id", "state", "updated_at", "github_pr_number"],
    )


def downgrade() -> None:
    op.drop_index("ix_pull_requests_repo_state_updated", table_name="pull_requests")
