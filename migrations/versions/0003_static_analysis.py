"""Static analysis engine: analysis_runs, analyzer_executions, findings, finding_sources

Revision ID: 0003_static_analysis
Revises: 0002_repository_intelligence
Create Date: 2026-08-15

"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003_static_analysis"
down_revision: str | None = "0002_repository_intelligence"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

_ANALYSIS_RUN_STATUS = sa.Enum(
    "pending", "running", "succeeded", "partial", "failed",
    name="analysis_run_status", native_enum=False, length=16,
)
_ANALYZER_EXECUTION_STATUS = sa.Enum(
    "succeeded", "failed", "timed_out", "unsupported", "skipped",
    name="analyzer_execution_status", native_enum=False, length=16,
)
_FINDING_CATEGORY = sa.Enum(
    "correctness", "security", "memory_safety", "resource_management", "concurrency",
    "performance", "maintainability", "style", "portability", "undefined_behavior",
    "api_misuse", "unknown",
    name="finding_category", native_enum=False, length=32,
)
_SEVERITY = sa.Enum(
    "critical", "high", "medium", "low", "info", name="severity", native_enum=False, length=16
)
_CONFIDENCE = sa.Enum("high", "medium", "low", name="confidence", native_enum=False, length=16)
_FINDING_STATUS = sa.Enum("active", name="finding_status", native_enum=False, length=16)


def upgrade() -> None:
    op.create_table(
        "analysis_runs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("repository_id", sa.Uuid(), nullable=False),
        sa.Column("repository_index_id", sa.Uuid(), nullable=False),
        sa.Column("pull_request_id", sa.Uuid(), nullable=True),
        sa.Column("commit_sha", sa.String(length=40), nullable=False),
        sa.Column("config_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("status", _ANALYSIS_RUN_STATUS, nullable=False),
        sa.Column("analyzers_succeeded", sa.Integer(), nullable=False),
        sa.Column("analyzers_failed", sa.Integer(), nullable=False),
        sa.Column("analyzers_skipped", sa.Integer(), nullable=False),
        sa.Column("raw_findings_count", sa.Integer(), nullable=False),
        sa.Column("findings_count", sa.Integer(), nullable=False),
        sa.Column("duration_ms", sa.Float(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["repository_id"], ["repositories.id"]),
        sa.ForeignKeyConstraint(["repository_index_id"], ["repository_indexes.id"]),
        sa.ForeignKeyConstraint(["pull_request_id"], ["pull_requests.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_analysis_runs_repository_id", "analysis_runs", ["repository_id"])
    op.create_index("ix_analysis_runs_repository_index_id", "analysis_runs", ["repository_index_id"])
    op.create_index("ix_analysis_runs_pull_request_id", "analysis_runs", ["pull_request_id"])
    op.create_index(
        "uq_analysis_runs_succeeded_identity", "analysis_runs",
        ["repository_id", "commit_sha", "config_fingerprint"],
        unique=True,
        postgresql_where=sa.text("status = 'succeeded'"),
        sqlite_where=sa.text("status = 'succeeded'"),
    )

    op.create_table(
        "analyzer_executions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("analysis_run_id", sa.Uuid(), nullable=False),
        sa.Column("analyzer", sa.String(length=64), nullable=False),
        sa.Column("version", sa.String(length=64), nullable=True),
        sa.Column("status", _ANALYZER_EXECUTION_STATUS, nullable=False),
        sa.Column("duration_ms", sa.Float(), nullable=False),
        sa.Column("raw_findings_count", sa.Integer(), nullable=False),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["analysis_run_id"], ["analysis_runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_analyzer_executions_analysis_run_id", "analyzer_executions", ["analysis_run_id"])

    op.create_table(
        "findings",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("analysis_run_id", sa.Uuid(), nullable=False),
        sa.Column("fingerprint", sa.String(length=64), nullable=False),
        sa.Column("rule_id", sa.String(length=128), nullable=False),
        sa.Column("category", _FINDING_CATEGORY, nullable=False),
        sa.Column("title", sa.String(length=512), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("severity", _SEVERITY, nullable=False),
        sa.Column("confidence", _CONFIDENCE, nullable=False),
        sa.Column("file_path", sa.String(length=1024), nullable=False),
        sa.Column("start_line", sa.Integer(), nullable=False),
        sa.Column("end_line", sa.Integer(), nullable=False),
        sa.Column("start_column", sa.Integer(), nullable=False),
        sa.Column("end_column", sa.Integer(), nullable=False),
        sa.Column("symbol_id", sa.Uuid(), nullable=True),
        sa.Column("symbol_name", sa.String(length=512), nullable=True),
        sa.Column("source_analyzer", sa.String(length=64), nullable=False),
        sa.Column("is_on_changed_line", sa.Boolean(), nullable=False),
        sa.Column("is_in_changed_symbol", sa.Boolean(), nullable=False),
        sa.Column("status", _FINDING_STATUS, nullable=False),
        sa.Column("raw_metadata", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["analysis_run_id"], ["analysis_runs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["symbol_id"], ["symbols.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_findings_analysis_run_id", "findings", ["analysis_run_id"])
    op.create_index("ix_findings_file_path", "findings", ["analysis_run_id", "file_path"])
    op.create_index("ix_findings_fingerprint", "findings", ["fingerprint"])
    op.create_index("ix_findings_severity", "findings", ["analysis_run_id", "severity"])
    op.create_index("ix_findings_symbol_id", "findings", ["symbol_id"])
    op.create_index("ix_findings_changed_line", "findings", ["analysis_run_id", "is_on_changed_line"])

    op.create_table(
        "finding_sources",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("finding_id", sa.Uuid(), nullable=False),
        sa.Column("analyzer", sa.String(length=64), nullable=False),
        sa.Column("rule_id", sa.String(length=128), nullable=False),
        sa.Column("fingerprint", sa.String(length=64), nullable=False),
        sa.Column("severity", _SEVERITY, nullable=False),
        sa.Column("confidence", _CONFIDENCE, nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("start_line", sa.Integer(), nullable=False),
        sa.Column("end_line", sa.Integer(), nullable=False),
        sa.Column("start_column", sa.Integer(), nullable=False),
        sa.Column("end_column", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["finding_id"], ["findings.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_finding_sources_finding_id", "finding_sources", ["finding_id"])


def downgrade() -> None:
    op.drop_index("ix_finding_sources_finding_id", table_name="finding_sources")
    op.drop_table("finding_sources")

    op.drop_index("ix_findings_changed_line", table_name="findings")
    op.drop_index("ix_findings_symbol_id", table_name="findings")
    op.drop_index("ix_findings_severity", table_name="findings")
    op.drop_index("ix_findings_fingerprint", table_name="findings")
    op.drop_index("ix_findings_file_path", table_name="findings")
    op.drop_index("ix_findings_analysis_run_id", table_name="findings")
    op.drop_table("findings")

    op.drop_index("ix_analyzer_executions_analysis_run_id", table_name="analyzer_executions")
    op.drop_table("analyzer_executions")

    op.drop_index("uq_analysis_runs_succeeded_identity", table_name="analysis_runs")
    op.drop_index("ix_analysis_runs_pull_request_id", table_name="analysis_runs")
    op.drop_index("ix_analysis_runs_repository_index_id", table_name="analysis_runs")
    op.drop_index("ix_analysis_runs_repository_id", table_name="analysis_runs")
    op.drop_table("analysis_runs")
