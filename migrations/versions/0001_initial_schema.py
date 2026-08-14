"""Initial schema: repositories, pull_requests, pull_request_ingestions

Revision ID: 0001_initial
Revises:
Create Date: 2026-08-14

"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001_initial"
down_revision: str | None = None
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "repositories",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("github_repository_id", sa.BigInteger(), nullable=False),
        sa.Column("owner", sa.String(length=255), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("full_name", sa.String(length=511), nullable=False),
        sa.Column("installation_id", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("github_repository_id", name="uq_repositories_github_repository_id"),
    )
    op.create_index(
        "ix_repositories_github_repository_id", "repositories", ["github_repository_id"]
    )

    op.create_table(
        "pull_requests",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("repository_id", sa.Uuid(), nullable=False),
        sa.Column("github_pr_number", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(length=1024), nullable=False),
        sa.Column("author", sa.String(length=255), nullable=False),
        sa.Column("base_sha", sa.String(length=40), nullable=False),
        sa.Column("head_sha", sa.String(length=40), nullable=False),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["repository_id"], ["repositories.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "repository_id", "github_pr_number", name="uq_pull_requests_repo_number"
        ),
    )
    op.create_index("ix_pull_requests_repository_id", "pull_requests", ["repository_id"])

    op.create_table(
        "pull_request_ingestions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("pull_request_id", sa.Uuid(), nullable=True),
        sa.Column("delivery_id", sa.String(length=255), nullable=False),
        sa.Column("event_action", sa.String(length=32), nullable=False),
        sa.Column("head_sha", sa.String(length=40), nullable=False),
        sa.Column(
            "status",
            sa.Enum("in_progress", "succeeded", "failed", name="ingestion_status", native_enum=False, length=32),
            nullable=False,
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["pull_request_id"], ["pull_requests.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("delivery_id", name="uq_pull_request_ingestions_delivery_id"),
    )
    op.create_index(
        "ix_pull_request_ingestions_delivery_id", "pull_request_ingestions", ["delivery_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_pull_request_ingestions_delivery_id", table_name="pull_request_ingestions")
    op.drop_table("pull_request_ingestions")
    op.drop_index("ix_pull_requests_repository_id", table_name="pull_requests")
    op.drop_table("pull_requests")
    op.drop_index("ix_repositories_github_repository_id", table_name="repositories")
    op.drop_table("repositories")
