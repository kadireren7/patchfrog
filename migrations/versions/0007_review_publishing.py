"""GitHub review publishing: review_publications, review_publication_comments

Revision ID: 0007_review_publishing
Revises: 0006_ai_reviewer
Create Date: 2026-08-17

"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007_review_publishing"
down_revision: str | None = "0006_ai_reviewer"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

_PUBLICATION_MODE = sa.Enum("dry_run", "publish", name="review_publication_mode", native_enum=False, length=16)
_PUBLICATION_STATUS = sa.Enum(
    "planned", "dry_run", "publishing", "published", "skipped_no_findings", "skipped_disabled",
    "stale", "failed", "reconciled", name="review_publication_status", native_enum=False, length=32,
)
_DISPOSITION = sa.Enum(
    "inline", "summary_only", "omitted", name="publication_disposition", native_enum=False, length=16
)
_SEVERITY = sa.Enum(
    "critical", "high", "medium", "low", "info", name="severity", native_enum=False, length=16
)


def upgrade() -> None:
    op.create_table(
        "review_publications",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("review_run_id", sa.Uuid(), nullable=False),
        sa.Column("repository_id", sa.Uuid(), nullable=False),
        sa.Column("pull_request_id", sa.Uuid(), nullable=True),
        sa.Column("pull_request_number", sa.Integer(), nullable=False),
        sa.Column("base_sha", sa.String(length=40), nullable=True),
        sa.Column("head_sha", sa.String(length=40), nullable=False),
        sa.Column("mode", _PUBLICATION_MODE, nullable=False),
        sa.Column("status", _PUBLICATION_STATUS, nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("github_review_id", sa.BigInteger(), nullable=True),
        sa.Column("inline_count", sa.Integer(), nullable=False),
        sa.Column("summary_only_count", sa.Integer(), nullable=False),
        sa.Column("omitted_count", sa.Integer(), nullable=False),
        sa.Column("reconciled", sa.Boolean(), nullable=False),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["review_run_id"], ["review_runs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["repository_id"], ["repositories.id"]),
        sa.ForeignKeyConstraint(["pull_request_id"], ["pull_requests.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_review_publications_review_run_id", "review_publications", ["review_run_id"])
    op.create_index("ix_review_publications_repository_id", "review_publications", ["repository_id"])
    op.create_index(
        "uq_review_publications_published_identity", "review_publications",
        ["review_run_id", "mode"],
        unique=True,
        postgresql_where=sa.text("status = 'published'"),
        sqlite_where=sa.text("status = 'published'"),
    )

    op.create_table(
        "review_publication_comments",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("review_publication_id", sa.Uuid(), nullable=False),
        sa.Column("finding_id", sa.Uuid(), nullable=False),
        sa.Column("fingerprint", sa.String(length=64), nullable=False),
        sa.Column("path", sa.String(length=1024), nullable=False),
        sa.Column("severity", _SEVERITY, nullable=False),
        sa.Column("disposition", _DISPOSITION, nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("side", sa.String(length=8), nullable=True),
        sa.Column("line", sa.Integer(), nullable=True),
        sa.Column("start_side", sa.String(length=8), nullable=True),
        sa.Column("start_line", sa.Integer(), nullable=True),
        sa.Column("body_hash", sa.String(length=64), nullable=True),
        sa.Column("github_comment_id", sa.BigInteger(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["review_publication_id"], ["review_publications.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["finding_id"], ["ai_findings.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_review_publication_comments_publication_id", "review_publication_comments", ["review_publication_id"]
    )
    op.create_index(
        "uq_review_publication_comments_fingerprint", "review_publication_comments",
        ["review_publication_id", "fingerprint"], unique=True,
    )


def downgrade() -> None:
    op.drop_index("uq_review_publication_comments_fingerprint", table_name="review_publication_comments")
    op.drop_index("ix_review_publication_comments_publication_id", table_name="review_publication_comments")
    op.drop_table("review_publication_comments")

    op.drop_index("uq_review_publications_published_identity", table_name="review_publications")
    op.drop_index("ix_review_publications_repository_id", table_name="review_publications")
    op.drop_index("ix_review_publications_review_run_id", table_name="review_publications")
    op.drop_table("review_publications")
