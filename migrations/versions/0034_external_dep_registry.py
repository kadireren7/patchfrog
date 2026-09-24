"""M5 external dependency + contract registry

Revision ID: 0034_external_dep_registry
Revises: 0033_review_cost_engine
Create Date: 2026-09-25

"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0034_external_dep_registry"
down_revision: str | None = "0033_review_cost_engine"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "external_dependencies",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "repository_id", sa.Uuid(), sa.ForeignKey("repositories.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("dependency_key", sa.String(length=512), nullable=False),
        sa.Column("provider_key", sa.String(length=255), nullable=False),
        sa.Column("display_name", sa.String(length=255), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("ecosystem", sa.String(length=16), nullable=False),
        sa.Column("package_name", sa.String(length=255), nullable=True),
        sa.Column("declared_version", sa.String(length=128), nullable=True),
        sa.Column("resolved_version", sa.String(length=128), nullable=True),
        sa.Column("confidence", sa.String(length=8), nullable=False),
        sa.Column("contract_source_type", sa.String(length=32), nullable=True),
        sa.Column("contract_source_ref", sa.String(length=1024), nullable=True),
        sa.Column("contract_fingerprint", sa.String(length=64), nullable=True),
        sa.Column("usage_site_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("usage_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("evidence_counts", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("metadata_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="active"),
        sa.Column("discovery_version", sa.Integer(), nullable=False),
        sa.Column("first_observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_observed_commit_sha", sa.String(length=64), nullable=True),
        sa.Column("removed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("repository_id", "dependency_key", name="uq_external_dependencies_repo_key"),
    )
    op.create_index("ix_external_dependencies_repo_status", "external_dependencies", ["repository_id", "status"])
    op.create_index("ix_external_dependencies_provider", "external_dependencies", ["provider_key"])

    op.create_table(
        "external_dependency_usage_sites",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "dependency_id",
            sa.Uuid(),
            sa.ForeignKey("external_dependencies.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("file_path", sa.String(length=1024), nullable=False),
        sa.Column("line", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("symbol", sa.String(length=1024), nullable=True),
        sa.Column("evidence_type", sa.String(length=32), nullable=False),
        sa.Column("token", sa.String(length=512), nullable=False),
        sa.Column("confidence", sa.String(length=8), nullable=False),
        sa.UniqueConstraint(
            "dependency_id", "file_path", "line", "evidence_type", "token",
            name="uq_external_dependency_usage_sites_identity",
        ),
    )
    op.create_index(
        "ix_external_dependency_usage_sites_dependency", "external_dependency_usage_sites", ["dependency_id"]
    )
    op.create_index("ix_external_dependency_usage_sites_file", "external_dependency_usage_sites", ["file_path"])

    op.create_table(
        "external_contract_snapshots",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "dependency_id",
            sa.Uuid(),
            sa.ForeignKey("external_dependencies.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("fingerprint", sa.String(length=64), nullable=False),
        sa.Column("algorithm", sa.String(length=16), nullable=False),
        sa.Column("normalization_version", sa.Integer(), nullable=False),
        sa.Column("source_type", sa.String(length=32), nullable=False),
        sa.Column("source_ref", sa.String(length=1024), nullable=False),
        sa.Column("declared_version", sa.String(length=128), nullable=True),
        sa.Column("resolved_version", sa.String(length=128), nullable=True),
        sa.Column("normalized_contract", sa.Text(), nullable=True),
        sa.Column("summary_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("first_observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("first_observed_commit_sha", sa.String(length=64), nullable=True),
        sa.Column("last_observed_commit_sha", sa.String(length=64), nullable=True),
        sa.UniqueConstraint(
            "dependency_id", "fingerprint", "normalization_version", name="uq_external_contract_snapshots_identity"
        ),
    )
    op.create_index(
        "ix_external_contract_snapshots_dependency",
        "external_contract_snapshots",
        ["dependency_id", "last_observed_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_external_contract_snapshots_dependency", table_name="external_contract_snapshots")
    op.drop_table("external_contract_snapshots")
    op.drop_index("ix_external_dependency_usage_sites_file", table_name="external_dependency_usage_sites")
    op.drop_index("ix_external_dependency_usage_sites_dependency", table_name="external_dependency_usage_sites")
    op.drop_table("external_dependency_usage_sites")
    op.drop_index("ix_external_dependencies_provider", table_name="external_dependencies")
    op.drop_index("ix_external_dependencies_repo_status", table_name="external_dependencies")
    op.drop_table("external_dependencies")
