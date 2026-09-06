"""Add Cross-Repo Intelligence Foundation tables and run-level summary columns

Revision ID: 0027_cross_repo_intelligence
Revises: 0026_cross_pr_peer_index
Create Date: 2026-09-06

"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0027_cross_repo_intelligence"
down_revision: str | None = "0026_cross_pr_peer_index"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "repository_contract_keys",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("repository_id", sa.Uuid(), nullable=False),
        sa.Column("contract_kind", sa.String(length=32), nullable=False),
        sa.Column("stable_key", sa.String(length=255), nullable=False),
        sa.Column("file_path", sa.String(length=1024), nullable=False),
        sa.Column("qualified_name", sa.String(length=2048), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["repository_id"], ["repositories.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("repository_id", "stable_key", name="uq_repository_contract_keys_repo_key"),
    )
    op.create_index(
        "ix_repository_contract_keys_repository_id", "repository_contract_keys", ["repository_id"]
    )
    op.create_index(
        "ix_repository_contract_keys_repo_symbol",
        "repository_contract_keys",
        ["repository_id", "file_path", "qualified_name"],
    )

    op.create_table(
        "repository_relations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("source_repository_id", sa.Uuid(), nullable=False),
        sa.Column("target_repository_id", sa.Uuid(), nullable=False),
        sa.Column("relation_kind", sa.String(length=32), nullable=False),
        sa.Column("external_contract_key", sa.String(length=255), nullable=False),
        sa.Column("provenance", sa.String(length=32), nullable=False),
        sa.Column("active", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["source_repository_id"], ["repositories.id"]),
        sa.ForeignKeyConstraint(["target_repository_id"], ["repositories.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "source_repository_id", "target_repository_id", "relation_kind", "external_contract_key",
            name="uq_repository_relations_identity",
        ),
    )
    op.create_index(
        "ix_repository_relations_source_repository_id", "repository_relations", ["source_repository_id"]
    )
    op.create_index(
        "ix_repository_relations_target_repository_id", "repository_relations", ["target_repository_id"]
    )
    op.create_index(
        "ix_repository_relations_source_active", "repository_relations", ["source_repository_id", "active"]
    )

    op.add_column("review_runs", sa.Column("cross_repo_peer_count", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("review_runs", sa.Column("cross_repo_signal_count", sa.Integer(), nullable=False, server_default="0"))
    op.add_column(
        "review_runs",
        sa.Column("cross_repo_explicit_contract_relation_count", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "review_runs", sa.Column("cross_repo_require_critic_count", sa.Integer(), nullable=False, server_default="0")
    )
    op.add_column(
        "review_runs", sa.Column("cross_repo_deepen_context_count", sa.Integer(), nullable=False, server_default="0")
    )


def downgrade() -> None:
    op.drop_column("review_runs", "cross_repo_deepen_context_count")
    op.drop_column("review_runs", "cross_repo_require_critic_count")
    op.drop_column("review_runs", "cross_repo_explicit_contract_relation_count")
    op.drop_column("review_runs", "cross_repo_signal_count")
    op.drop_column("review_runs", "cross_repo_peer_count")

    op.drop_index("ix_repository_relations_source_active", table_name="repository_relations")
    op.drop_index("ix_repository_relations_target_repository_id", table_name="repository_relations")
    op.drop_index("ix_repository_relations_source_repository_id", table_name="repository_relations")
    op.drop_table("repository_relations")

    op.drop_index("ix_repository_contract_keys_repo_symbol", table_name="repository_contract_keys")
    op.drop_index("ix_repository_contract_keys_repository_id", table_name="repository_contract_keys")
    op.drop_table("repository_contract_keys")
