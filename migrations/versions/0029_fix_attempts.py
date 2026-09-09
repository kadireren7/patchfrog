"""Add Fix Attempts table (Milestone T -- Agent Handoff / MCP, T3)

Revision ID: 0029_fix_attempts
Revises: 0028_executable_verification
Create Date: 2026-09-09

"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0029_fix_attempts"
down_revision: str | None = "0028_executable_verification"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "fix_attempts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("handoff_id", sa.String(length=64), nullable=False),
        sa.Column("finding_id", sa.Uuid(), nullable=False),
        sa.Column("repository_id", sa.Uuid(), nullable=False),
        sa.Column("original_commit_sha", sa.String(length=40), nullable=False),
        sa.Column("candidate_fix_commit_sha", sa.String(length=40), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("deterministic_evidence", sa.Text(), server_default="[]", nullable=False),
        sa.Column("executable_verification_outcome", sa.String(length=32), nullable=True),
        sa.Column("remaining_issue_summary", sa.Text(), nullable=True),
        sa.Column("limitations", sa.Text(), server_default="[]", nullable=False),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["finding_id"], ["ai_findings.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["repository_id"], ["repositories.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "handoff_id", "candidate_fix_commit_sha", name="uq_fix_attempts_handoff_candidate"
        ),
    )
    op.create_index("ix_fix_attempts_finding_id", "fix_attempts", ["finding_id"])
    op.create_index(
        "ix_fix_attempts_repository_id_status", "fix_attempts", ["repository_id", "status"]
    )


def downgrade() -> None:
    op.drop_index("ix_fix_attempts_repository_id_status", table_name="fix_attempts")
    op.drop_index("ix_fix_attempts_finding_id", table_name="fix_attempts")
    op.drop_table("fix_attempts")
