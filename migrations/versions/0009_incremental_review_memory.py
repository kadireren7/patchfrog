"""Incremental review memory: review_runs.incremental_context_fingerprint,
review_generations, review_memory_findings, review_memory_transitions

Revision ID: 0009_incremental_review_memory
Revises: 0008_publication_policy_fp
Create Date: 2026-08-20

"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009_incremental_review_memory"
down_revision: str | None = "0008_publication_policy_fp"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

#: Must match patchfrog.review_memory.config.NO_MEMORY_CONTEXT_FINGERPRINT
#: exactly -- sha256(b"incremental:full:no-previous-generation:v1").
_NO_MEMORY_CONTEXT_FINGERPRINT = "c989c72dee8113e2c1410f9d68c2cb3276e63f08490a2e6607baf104df7e3e0c"

_INCREMENTAL_RUN_MODE = sa.Enum("full", "incremental", name="incremental_run_mode", native_enum=False, length=16)
_TRANSITION_REASON_CODE = sa.Enum(
    "symbol_unchanged", "line_only_moved", "file_renamed", "symbol_modified", "evidence_region_changed",
    "symbol_deleted", "file_deleted", "history_rewritten", "ambiguous_symbol_match", "previous_finding_missing",
    "new_finding", "base_changed", "toolchain_drift", "model_drift", "no_previous_review",
    "partial_previous_review", "recheck_confirmed", "recheck_no_longer_present", "ancestry_unverifiable",
    name="transition_reason_code", native_enum=False, length=32,
)
_FINDING_MEMORY_STATUS = sa.Enum(
    "open", "carried_forward", "changed", "resolved", "superseded", "ambiguous",
    name="finding_memory_status", native_enum=False, length=16,
)
_SYMBOL_KIND = sa.Enum(
    "module", "function", "method", "class", "struct", "enum", "union", "interface", "variable",
    "constant", "type_alias", "macro",
    name="symbol_kind", native_enum=False, length=16,
)
_FINDING_CATEGORY = sa.Enum(
    "correctness", "security", "memory_safety", "resource_management", "concurrency",
    "performance", "maintainability", "style", "portability", "undefined_behavior",
    "api_misuse", "unknown", name="finding_category", native_enum=False, length=32,
)
_SEVERITY = sa.Enum(
    "critical", "high", "medium", "low", "info", name="severity", native_enum=False, length=16
)


def upgrade() -> None:
    # -- review_runs.incremental_context_fingerprint --
    op.add_column(
        "review_runs",
        sa.Column("incremental_context_fingerprint", sa.String(length=64), nullable=True),
    )
    op.execute(
        sa.text(
            "UPDATE review_runs SET incremental_context_fingerprint = :fp "
            "WHERE incremental_context_fingerprint IS NULL"
        ).bindparams(fp=_NO_MEMORY_CONTEXT_FINGERPRINT)
    )
    op.alter_column("review_runs", "incremental_context_fingerprint", nullable=False)

    op.drop_index("uq_review_runs_succeeded_identity", table_name="review_runs")
    op.create_index(
        "uq_review_runs_succeeded_identity", "review_runs",
        ["repository_id", "commit_sha", "config_fingerprint", "model_fingerprint", "incremental_context_fingerprint"],
        unique=True,
        postgresql_where=sa.text("status = 'succeeded'"),
        sqlite_where=sa.text("status = 'succeeded'"),
    )

    # -- review_generations --
    op.create_table(
        "review_generations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("repository_id", sa.Uuid(), nullable=False),
        sa.Column("pull_request_id", sa.Uuid(), nullable=False),
        sa.Column("sequence_number", sa.Integer(), nullable=False),
        sa.Column("review_run_id", sa.Uuid(), nullable=False),
        sa.Column("commit_sha", sa.String(length=40), nullable=False),
        sa.Column("previous_generation_id", sa.Uuid(), nullable=True),
        sa.Column("previous_commit_sha", sa.String(length=40), nullable=True),
        sa.Column("ancestry_verified", sa.Boolean(), nullable=False),
        sa.Column("mode", _INCREMENTAL_RUN_MODE, nullable=False),
        sa.Column("compatibility_ok", sa.Boolean(), nullable=False),
        sa.Column("invalidation_reason", _TRANSITION_REASON_CODE, nullable=True),
        sa.Column("memory_compatibility_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["repository_id"], ["repositories.id"]),
        sa.ForeignKeyConstraint(["pull_request_id"], ["pull_requests.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["review_run_id"], ["review_runs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["previous_generation_id"], ["review_generations.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_review_generations_repo_pr", "review_generations", ["repository_id", "pull_request_id"])
    op.create_index("ix_review_generations_review_run_id", "review_generations", ["review_run_id"], unique=True)
    op.create_index("ix_review_generations_previous", "review_generations", ["previous_generation_id"])
    op.create_index(
        "uq_review_generations_pr_sequence", "review_generations", ["pull_request_id", "sequence_number"],
        unique=True,
    )

    # -- review_memory_findings --
    op.create_table(
        "review_memory_findings",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("repository_id", sa.Uuid(), nullable=False),
        sa.Column("pull_request_id", sa.Uuid(), nullable=False),
        sa.Column("source_review_run_id", sa.Uuid(), nullable=False),
        sa.Column("source_finding_id", sa.Uuid(), nullable=False),
        sa.Column("current_finding_id", sa.Uuid(), nullable=True),
        sa.Column("current_review_run_id", sa.Uuid(), nullable=True),
        sa.Column("first_seen_commit_sha", sa.String(length=40), nullable=False),
        sa.Column("last_seen_commit_sha", sa.String(length=40), nullable=False),
        sa.Column("file_path", sa.String(length=1024), nullable=False),
        sa.Column("symbol_id", sa.Uuid(), nullable=True),
        sa.Column("symbol_qualified_name", sa.String(length=2048), nullable=True),
        sa.Column("symbol_kind", _SYMBOL_KIND, nullable=True),
        sa.Column("category", _FINDING_CATEGORY, nullable=False),
        sa.Column("severity", _SEVERITY, nullable=False),
        sa.Column("title", sa.String(length=512), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("start_line", sa.Integer(), nullable=False),
        sa.Column("end_line", sa.Integer(), nullable=False),
        sa.Column("exact_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("semantic_family_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("status", _FINDING_MEMORY_STATUS, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["repository_id"], ["repositories.id"]),
        sa.ForeignKeyConstraint(["pull_request_id"], ["pull_requests.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["source_review_run_id"], ["review_runs.id"]),
        sa.ForeignKeyConstraint(["source_finding_id"], ["ai_findings.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["current_finding_id"], ["ai_findings.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["current_review_run_id"], ["review_runs.id"]),
        sa.ForeignKeyConstraint(["symbol_id"], ["symbols.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_review_memory_findings_repo_pr", "review_memory_findings", ["repository_id", "pull_request_id"])
    op.create_index("ix_review_memory_findings_status", "review_memory_findings", ["pull_request_id", "status"])
    op.create_index(
        "uq_review_memory_findings_active_family", "review_memory_findings",
        ["pull_request_id", "semantic_family_fingerprint"],
        unique=True,
        postgresql_where=sa.text("status IN ('open', 'carried_forward', 'changed', 'ambiguous')"),
        sqlite_where=sa.text("status IN ('open', 'carried_forward', 'changed', 'ambiguous')"),
    )

    # -- review_memory_transitions --
    op.create_table(
        "review_memory_transitions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("memory_finding_id", sa.Uuid(), nullable=False),
        sa.Column("source_review_run_id", sa.Uuid(), nullable=True),
        sa.Column("target_review_run_id", sa.Uuid(), nullable=False),
        sa.Column("old_status", _FINDING_MEMORY_STATUS, nullable=True),
        sa.Column("new_status", _FINDING_MEMORY_STATUS, nullable=False),
        sa.Column("reason", _TRANSITION_REASON_CODE, nullable=False),
        sa.Column("detail", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["memory_finding_id"], ["review_memory_findings.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["source_review_run_id"], ["review_runs.id"]),
        sa.ForeignKeyConstraint(["target_review_run_id"], ["review_runs.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_review_memory_transitions_finding", "review_memory_transitions", ["memory_finding_id"])
    op.create_index("ix_review_memory_transitions_target_run", "review_memory_transitions", ["target_review_run_id"])


def downgrade() -> None:
    op.drop_index("ix_review_memory_transitions_target_run", table_name="review_memory_transitions")
    op.drop_index("ix_review_memory_transitions_finding", table_name="review_memory_transitions")
    op.drop_table("review_memory_transitions")

    op.drop_index("uq_review_memory_findings_active_family", table_name="review_memory_findings")
    op.drop_index("ix_review_memory_findings_status", table_name="review_memory_findings")
    op.drop_index("ix_review_memory_findings_repo_pr", table_name="review_memory_findings")
    op.drop_table("review_memory_findings")

    op.drop_index("uq_review_generations_pr_sequence", table_name="review_generations")
    op.drop_index("ix_review_generations_previous", table_name="review_generations")
    op.drop_index("ix_review_generations_review_run_id", table_name="review_generations")
    op.drop_index("ix_review_generations_repo_pr", table_name="review_generations")
    op.drop_table("review_generations")

    op.drop_index("uq_review_runs_succeeded_identity", table_name="review_runs")
    op.create_index(
        "uq_review_runs_succeeded_identity", "review_runs",
        ["repository_id", "commit_sha", "config_fingerprint", "model_fingerprint"],
        unique=True,
        postgresql_where=sa.text("status = 'succeeded'"),
        sqlite_where=sa.text("status = 'succeeded'"),
    )
    op.drop_column("review_runs", "incremental_context_fingerprint")
