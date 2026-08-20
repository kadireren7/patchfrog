"""review_memory_findings.evidence -- deterministic evidence identity
for zero-AI-call carry-forward (patchfrog.review_memory.evidence)

Revision ID: 0010_review_memory_evidence
Revises: 0009_incremental_review_memory
Create Date: 2026-08-21

"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0010_review_memory_evidence"
down_revision: str | None = "0009_incremental_review_memory"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "review_memory_findings",
        sa.Column("evidence", sa.Text(), nullable=False, server_default="[]"),
    )


def downgrade() -> None:
    op.drop_column("review_memory_findings", "evidence")
