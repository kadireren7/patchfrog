"""Add nullable impact column to ai_finding_proposals/ai_findings

Revision ID: 0011_security_review_quality
Revises: 0010_review_memory_evidence
Create Date: 2026-08-22

"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0011_security_review_quality"
down_revision: str | None = "0010_review_memory_evidence"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("ai_finding_proposals", sa.Column("impact", sa.Text(), nullable=True))
    op.add_column("ai_findings", sa.Column("impact", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("ai_findings", "impact")
    op.drop_column("ai_finding_proposals", "impact")
