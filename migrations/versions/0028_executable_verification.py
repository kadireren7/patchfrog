"""Add Executable Verification Foundation run-level summary columns

Revision ID: 0028_executable_verification
Revises: 0027_cross_repo_intelligence
Create Date: 2026-09-07

"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0028_executable_verification"
down_revision: str | None = "0027_cross_repo_intelligence"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "review_runs",
        sa.Column("executable_verification_attempted_count", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "review_runs",
        sa.Column(
            "executable_verification_confirmed_failure_count", sa.Integer(), nullable=False, server_default="0"
        ),
    )
    op.add_column(
        "review_runs",
        sa.Column("executable_verification_passed_count", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "review_runs",
        sa.Column("executable_verification_timeout_count", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "review_runs",
        sa.Column("executable_verification_unsupported_count", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "review_runs",
        sa.Column("executable_verification_inconclusive_count", sa.Integer(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    op.drop_column("review_runs", "executable_verification_inconclusive_count")
    op.drop_column("review_runs", "executable_verification_unsupported_count")
    op.drop_column("review_runs", "executable_verification_timeout_count")
    op.drop_column("review_runs", "executable_verification_passed_count")
    op.drop_column("review_runs", "executable_verification_confirmed_failure_count")
    op.drop_column("review_runs", "executable_verification_attempted_count")
