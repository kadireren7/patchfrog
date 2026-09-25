"""M6 upstream change history + M7 migration plans/patches

Revision ID: 0035_upstream_change_migration
Revises: 0034_external_dep_registry
Create Date: 2026-09-25

"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0035_upstream_change_migration"
down_revision: str | None = "0034_external_dep_registry"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "external_change_events",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("fingerprint", sa.String(length=64), nullable=False),
        sa.Column("engine_version", sa.Integer(), nullable=False),
        sa.Column("provider_key", sa.String(length=255), nullable=True),
        sa.Column("ecosystem", sa.String(length=16), nullable=True),
        sa.Column("package_name", sa.String(length=255), nullable=True),
        sa.Column("dependency_key", sa.String(length=512), nullable=True),
        sa.Column("target_label", sa.String(length=255), nullable=False),
        sa.Column("source_type", sa.String(length=32), nullable=False),
        sa.Column("change_kind", sa.String(length=32), nullable=False),
        sa.Column("source_ref", sa.String(length=1024), nullable=False),
        sa.Column("old_version", sa.String(length=128), nullable=True),
        sa.Column("new_version", sa.String(length=128), nullable=True),
        sa.Column("old_contract_fingerprint", sa.String(length=64), nullable=True),
        sa.Column("new_contract_fingerprint", sa.String(length=64), nullable=True),
        sa.Column("contract_format", sa.String(length=16), nullable=True),
        sa.Column("risk", sa.String(length=24), nullable=False),
        sa.Column("compatibility", sa.String(length=24), nullable=False),
        sa.Column("reasons_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("counts_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("diff_item_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("truncated", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("hints_fingerprint", sa.String(length=64), nullable=True),
        sa.Column("hints_provenance", sa.String(length=200), nullable=True),
        sa.Column("release_json", sa.Text(), nullable=True),
        sa.Column("notes_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("first_observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("fingerprint", name="uq_external_change_events_fingerprint"),
    )
    op.create_index("ix_external_change_events_package", "external_change_events", ["ecosystem", "package_name"])
    op.create_index("ix_external_change_events_provider", "external_change_events", ["provider_key"])
    op.create_index("ix_external_change_events_dependency_key", "external_change_events", ["dependency_key"])

    op.create_table(
        "external_change_diff_items",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "event_id", sa.Uuid(), sa.ForeignKey("external_change_events.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("item_key", sa.String(length=16), nullable=False),
        sa.Column("kind", sa.String(length=48), nullable=False),
        sa.Column("location", sa.String(length=1024), nullable=False),
        sa.Column("subject_json", sa.Text(), nullable=False),
        sa.Column("compatibility", sa.String(length=24), nullable=False),
        sa.Column("old_repr", sa.Text(), nullable=True),
        sa.Column("new_repr", sa.Text(), nullable=True),
        sa.Column("explanation", sa.Text(), nullable=False),
        sa.Column("evidence_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("replacement", sa.String(length=512), nullable=True),
        sa.UniqueConstraint("event_id", "item_key", name="uq_external_change_diff_items_identity"),
    )
    op.create_index("ix_external_change_diff_items_event", "external_change_diff_items", ["event_id"])

    op.create_table(
        "external_change_impacts",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "event_id", sa.Uuid(), sa.ForeignKey("external_change_events.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column(
            "repository_id", sa.Uuid(), sa.ForeignKey("repositories.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column(
            "dependency_id", sa.Uuid(), sa.ForeignKey("external_dependencies.id", ondelete="SET NULL"), nullable=True
        ),
        sa.Column("dependency_key", sa.String(length=512), nullable=False, server_default=""),
        sa.Column("analyzed_commit_sha", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("reason", sa.String(length=255), nullable=False),
        sa.Column("direct_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("transitive_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("potential_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("related_test_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("consumers_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("blast_radius_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("first_analyzed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_analyzed_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "event_id", "repository_id", "dependency_key", "analyzed_commit_sha",
            name="uq_external_change_impacts_identity",
        ),
    )
    op.create_index("ix_external_change_impacts_repository", "external_change_impacts", ["repository_id", "status"])
    op.create_index("ix_external_change_impacts_event", "external_change_impacts", ["event_id"])

    op.create_table(
        "migration_plans",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "event_id", sa.Uuid(), sa.ForeignKey("external_change_events.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column(
            "repository_id", sa.Uuid(), sa.ForeignKey("repositories.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("plan_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("engine_version", sa.Integer(), nullable=False),
        sa.Column("base_commit_sha", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("base_content_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("dependency_keys_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("residual_risk", sa.String(length=16), nullable=False),
        sa.Column("step_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("auto_step_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("human_step_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("steps_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("unresolved_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("evidence_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_generated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("plan_fingerprint", name="uq_migration_plans_fingerprint"),
    )
    op.create_index("ix_migration_plans_event_repository", "migration_plans", ["event_id", "repository_id"])

    op.create_table(
        "migration_patches",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("plan_id", sa.Uuid(), sa.ForeignKey("migration_plans.id", ondelete="CASCADE"), nullable=False),
        sa.Column("patch_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("origin", sa.String(length=24), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("unified_diff", sa.Text(), nullable=True),
        sa.Column("modified_files_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("linkage_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("safety_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("plan_id", "patch_fingerprint", name="uq_migration_patches_identity"),
    )
    op.create_index("ix_migration_patches_plan", "migration_patches", ["plan_id"])


def downgrade() -> None:
    op.drop_index("ix_migration_patches_plan", table_name="migration_patches")
    op.drop_table("migration_patches")
    op.drop_index("ix_migration_plans_event_repository", table_name="migration_plans")
    op.drop_table("migration_plans")
    op.drop_index("ix_external_change_impacts_event", table_name="external_change_impacts")
    op.drop_index("ix_external_change_impacts_repository", table_name="external_change_impacts")
    op.drop_table("external_change_impacts")
    op.drop_index("ix_external_change_diff_items_event", table_name="external_change_diff_items")
    op.drop_table("external_change_diff_items")
    op.drop_index("ix_external_change_events_dependency_key", table_name="external_change_events")
    op.drop_index("ix_external_change_events_provider", table_name="external_change_events")
    op.drop_index("ix_external_change_events_package", table_name="external_change_events")
    op.drop_table("external_change_events")
