"""Context engine: context_bundles, context_items

Revision ID: 0005_context_engine
Revises: 0004_toolchain_identity
Create Date: 2026-08-16

"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005_context_engine"
down_revision: str | None = "0004_toolchain_identity"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

_BUNDLE_STATUS = sa.Enum(
    "running", "succeeded", "failed", name="context_bundle_status", native_enum=False, length=16
)
_TARGET_TYPE = sa.Enum("finding", "symbol", "line", name="context_target_type", native_enum=False, length=16)
_ITEM_KIND = sa.Enum(
    "target_symbol", "target_file_region", "caller", "callee", "imported_dependency",
    "included_header", "related_test", "parent_symbol", "sibling_symbol",
    name="context_item_kind", native_enum=False, length=32,
)
_RELATIONSHIP = sa.Enum(
    "target_symbol", "direct_caller", "transitive_caller", "direct_callee", "transitive_callee",
    "tests_target_file", "import_dependency", "include_dependency", "parent_symbol", "sibling_symbol",
    name="context_relationship", native_enum=False, length=32,
)


def upgrade() -> None:
    op.create_table(
        "context_bundles",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("repository_id", sa.Uuid(), nullable=False),
        sa.Column("repository_index_id", sa.Uuid(), nullable=False),
        sa.Column("analysis_run_id", sa.Uuid(), nullable=True),
        sa.Column("finding_id", sa.Uuid(), nullable=True),
        sa.Column("target_symbol_id", sa.Uuid(), nullable=True),
        sa.Column("commit_sha", sa.String(length=40), nullable=False),
        sa.Column("target_type", _TARGET_TYPE, nullable=False),
        sa.Column("target_file_path", sa.String(length=1024), nullable=False),
        sa.Column("target_line", sa.Integer(), nullable=True),
        sa.Column("target_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("config_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("engine_version", sa.Integer(), nullable=False),
        sa.Column("status", _BUNDLE_STATUS, nullable=False),
        sa.Column("candidate_count", sa.Integer(), nullable=False),
        sa.Column("selected_count", sa.Integer(), nullable=False),
        sa.Column("dropped_budget", sa.Integer(), nullable=False),
        sa.Column("dropped_overlap", sa.Integer(), nullable=False),
        sa.Column("dropped_duplicate", sa.Integer(), nullable=False),
        sa.Column("total_tokens", sa.Integer(), nullable=False),
        sa.Column("total_lines", sa.Integer(), nullable=False),
        sa.Column("generation_ms", sa.Float(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["repository_id"], ["repositories.id"]),
        sa.ForeignKeyConstraint(["repository_index_id"], ["repository_indexes.id"]),
        sa.ForeignKeyConstraint(["analysis_run_id"], ["analysis_runs.id"]),
        sa.ForeignKeyConstraint(["finding_id"], ["findings.id"]),
        sa.ForeignKeyConstraint(["target_symbol_id"], ["symbols.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_context_bundles_repository_id", "context_bundles", ["repository_id"])
    op.create_index("ix_context_bundles_repository_index_id", "context_bundles", ["repository_index_id"])
    op.create_index("ix_context_bundles_analysis_run_id", "context_bundles", ["analysis_run_id"])
    op.create_index("ix_context_bundles_finding_id", "context_bundles", ["finding_id"])
    op.create_index("ix_context_bundles_target_symbol_id", "context_bundles", ["target_symbol_id"])
    op.create_index(
        "uq_context_bundles_succeeded_identity", "context_bundles",
        ["repository_id", "commit_sha", "target_fingerprint", "config_fingerprint"],
        unique=True,
        postgresql_where=sa.text("status = 'succeeded'"),
        sqlite_where=sa.text("status = 'succeeded'"),
    )

    op.create_table(
        "context_items",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("bundle_id", sa.Uuid(), nullable=False),
        sa.Column("rank", sa.Integer(), nullable=False),
        sa.Column("kind", _ITEM_KIND, nullable=False),
        sa.Column("relationship", _RELATIONSHIP, nullable=False),
        sa.Column("distance", sa.Integer(), nullable=False),
        sa.Column("file_path", sa.String(length=1024), nullable=False),
        sa.Column("symbol_id", sa.Uuid(), nullable=True),
        sa.Column("symbol_name", sa.String(length=512), nullable=True),
        sa.Column("qualified_name", sa.String(length=2048), nullable=True),
        sa.Column("start_line", sa.Integer(), nullable=False),
        sa.Column("end_line", sa.Integer(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("truncated", sa.Boolean(), nullable=False),
        sa.Column("score", sa.Float(), nullable=False),
        sa.Column("score_breakdown", sa.Text(), nullable=False),
        sa.Column("estimated_tokens", sa.Integer(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["bundle_id"], ["context_bundles.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["symbol_id"], ["symbols.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_context_items_bundle_id", "context_items", ["bundle_id"])
    op.create_index("ix_context_items_symbol_id", "context_items", ["symbol_id"])
    op.create_index("ix_context_items_kind", "context_items", ["bundle_id", "kind"])


def downgrade() -> None:
    op.drop_index("ix_context_items_kind", table_name="context_items")
    op.drop_index("ix_context_items_symbol_id", table_name="context_items")
    op.drop_index("ix_context_items_bundle_id", table_name="context_items")
    op.drop_table("context_items")

    op.drop_index("uq_context_bundles_succeeded_identity", table_name="context_bundles")
    op.drop_index("ix_context_bundles_target_symbol_id", table_name="context_bundles")
    op.drop_index("ix_context_bundles_finding_id", table_name="context_bundles")
    op.drop_index("ix_context_bundles_analysis_run_id", table_name="context_bundles")
    op.drop_index("ix_context_bundles_repository_index_id", table_name="context_bundles")
    op.drop_index("ix_context_bundles_repository_id", table_name="context_bundles")
    op.drop_table("context_bundles")
