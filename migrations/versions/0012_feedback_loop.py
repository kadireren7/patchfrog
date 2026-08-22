"""Add feedback_events and feedback_assessments tables

Revision ID: 0012_feedback_loop
Revises: 0011_security_review_quality
Create Date: 2026-08-22

"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0012_feedback_loop"
down_revision: str | None = "0011_security_review_quality"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "feedback_events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("repository_id", sa.Uuid(), nullable=False),
        sa.Column("pull_request_id", sa.Uuid(), nullable=True),
        sa.Column("review_run_id", sa.Uuid(), nullable=True),
        sa.Column("publication_id", sa.Uuid(), nullable=True),
        sa.Column("review_publication_comment_id", sa.Uuid(), nullable=True),
        sa.Column("finding_id", sa.Uuid(), nullable=True),
        sa.Column("github_review_id", sa.BigInteger(), nullable=True),
        sa.Column("github_comment_id", sa.BigInteger(), nullable=True),
        sa.Column("event_type", sa.String(length=32), nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("external_event_id", sa.String(length=256), nullable=False),
        sa.Column("raw_signal", sa.Text(), nullable=False),
        sa.Column("normalized_signal", sa.String(length=64), nullable=False),
        sa.Column("signal_strength", sa.String(length=16), nullable=False),
        sa.Column("actor_login", sa.String(length=255), nullable=False),
        sa.Column("actor_is_bot", sa.Boolean(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ingested_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("event_metadata", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("engine_version", sa.Integer(), nullable=False, server_default="1"),
        sa.ForeignKeyConstraint(["repository_id"], ["repositories.id"]),
        sa.ForeignKeyConstraint(["pull_request_id"], ["pull_requests.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["review_run_id"], ["review_runs.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["publication_id"], ["review_publications.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(
            ["review_publication_comment_id"], ["review_publication_comments.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(["finding_id"], ["ai_findings.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_feedback_events_repository_id", "feedback_events", ["repository_id"])
    op.create_index("ix_feedback_events_pull_request_id", "feedback_events", ["pull_request_id"])
    op.create_index("ix_feedback_events_finding_id", "feedback_events", ["finding_id"])
    op.create_index(
        "ix_feedback_events_review_publication_comment_id",
        "feedback_events",
        ["review_publication_comment_id"],
    )
    op.create_index(
        "uq_feedback_events_external_identity",
        "feedback_events",
        ["source", "event_type", "external_event_id"],
        unique=True,
    )

    op.create_table(
        "feedback_assessments",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("finding_id", sa.Uuid(), nullable=False),
        sa.Column("assessment_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("usefulness_signal", sa.String(length=16), nullable=False),
        sa.Column("correctness_signal", sa.String(length=16), nullable=False),
        sa.Column("resolution_signal", sa.String(length=16), nullable=False),
        sa.Column("engagement_signal", sa.Boolean(), nullable=False),
        sa.Column("confidence", sa.String(length=16), nullable=True),
        sa.Column("reasons", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("positive_reactions", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("negative_reactions", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("developer_replies", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("explicit_useful", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("explicit_false_positive", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("explicit_fixed", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("explicit_ignore", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("thread_resolved", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("finding_changed", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("finding_disappeared", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column(
            "computed_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["finding_id"], ["ai_findings.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_feedback_assessments_finding_version",
        "feedback_assessments",
        ["finding_id", "assessment_version"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_table("feedback_assessments")
    op.drop_table("feedback_events")
