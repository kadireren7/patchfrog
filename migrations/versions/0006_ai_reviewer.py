"""AI reviewer: review_runs, review_candidates, ai_finding_proposals, critic_verdicts, ai_findings

Revision ID: 0006_ai_reviewer
Revises: 0005_context_engine
Create Date: 2026-08-16

"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006_ai_reviewer"
down_revision: str | None = "0005_context_engine"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

_REVIEW_RUN_STATUS = sa.Enum(
    "running", "succeeded", "partial", "failed", name="review_run_status", native_enum=False, length=16
)
_REVIEW_CANDIDATE_REASON = sa.Enum(
    "changed_symbol", "changed_module_region", "static_finding_evidence",
    name="review_candidate_reason", native_enum=False, length=32,
)
_REVIEW_CANDIDATE_STATUS = sa.Enum(
    "pending", "reviewed", "skipped_budget", "failed",
    name="review_candidate_status", native_enum=False, length=16,
)
_PROPOSAL_STATUS = sa.Enum(
    "accepted", "rejected_validation", "rejected_critic", "rejected_low_confidence",
    "suppressed_duplicate", name="proposal_status", native_enum=False, length=32,
)
_CRITIC_DECISION = sa.Enum(
    "accept", "reject", "downgrade", name="critic_decision", native_enum=False, length=16
)
_FINDING_CATEGORY = sa.Enum(
    "correctness", "security", "memory_safety", "resource_management", "concurrency",
    "performance", "maintainability", "style", "portability", "undefined_behavior",
    "api_misuse", "unknown", name="finding_category", native_enum=False, length=32,
)
_SEVERITY = sa.Enum(
    "critical", "high", "medium", "low", "info", name="severity", native_enum=False, length=16
)
_CONFIDENCE = sa.Enum("high", "medium", "low", name="confidence", native_enum=False, length=16)


def upgrade() -> None:
    op.create_table(
        "review_runs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("repository_id", sa.Uuid(), nullable=False),
        sa.Column("repository_index_id", sa.Uuid(), nullable=False),
        sa.Column("analysis_run_id", sa.Uuid(), nullable=True),
        sa.Column("pull_request_id", sa.Uuid(), nullable=True),
        sa.Column("commit_sha", sa.String(length=40), nullable=False),
        sa.Column("config_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("model_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("status", _REVIEW_RUN_STATUS, nullable=False),
        sa.Column("reviewer_provider", sa.String(length=64), nullable=False),
        sa.Column("reviewer_model", sa.String(length=128), nullable=False),
        sa.Column("critic_provider", sa.String(length=64), nullable=True),
        sa.Column("critic_model", sa.String(length=128), nullable=True),
        sa.Column("candidate_count", sa.Integer(), nullable=False),
        sa.Column("candidates_reviewed", sa.Integer(), nullable=False),
        sa.Column("candidates_failed", sa.Integer(), nullable=False),
        sa.Column("candidates_skipped_budget", sa.Integer(), nullable=False),
        sa.Column("proposals_count", sa.Integer(), nullable=False),
        sa.Column("accepted_count", sa.Integer(), nullable=False),
        sa.Column("rejected_count", sa.Integer(), nullable=False),
        sa.Column("suppressed_duplicate_count", sa.Integer(), nullable=False),
        sa.Column("reviewer_input_tokens", sa.Integer(), nullable=False),
        sa.Column("reviewer_output_tokens", sa.Integer(), nullable=False),
        sa.Column("critic_input_tokens", sa.Integer(), nullable=False),
        sa.Column("critic_output_tokens", sa.Integer(), nullable=False),
        sa.Column("duration_ms", sa.Float(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["repository_id"], ["repositories.id"]),
        sa.ForeignKeyConstraint(["repository_index_id"], ["repository_indexes.id"]),
        sa.ForeignKeyConstraint(["analysis_run_id"], ["analysis_runs.id"]),
        sa.ForeignKeyConstraint(["pull_request_id"], ["pull_requests.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_review_runs_repository_id", "review_runs", ["repository_id"])
    op.create_index("ix_review_runs_repository_index_id", "review_runs", ["repository_index_id"])
    op.create_index("ix_review_runs_pull_request_id", "review_runs", ["pull_request_id"])
    op.create_index(
        "uq_review_runs_succeeded_identity", "review_runs",
        ["repository_id", "commit_sha", "config_fingerprint", "model_fingerprint"],
        unique=True,
        postgresql_where=sa.text("status = 'succeeded'"),
        sqlite_where=sa.text("status = 'succeeded'"),
    )

    op.create_table(
        "review_candidates",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("review_run_id", sa.Uuid(), nullable=False),
        sa.Column("file_path", sa.String(length=1024), nullable=False),
        sa.Column("symbol_id", sa.Uuid(), nullable=True),
        sa.Column("symbol_name", sa.String(length=512), nullable=True),
        sa.Column("qualified_name", sa.String(length=2048), nullable=True),
        sa.Column("start_line", sa.Integer(), nullable=False),
        sa.Column("end_line", sa.Integer(), nullable=False),
        sa.Column("changed_lines", sa.Text(), nullable=False),
        sa.Column("reason", _REVIEW_CANDIDATE_REASON, nullable=False),
        sa.Column("static_finding_ids", sa.Text(), nullable=False),
        sa.Column("status", _REVIEW_CANDIDATE_STATUS, nullable=False),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("context_bundle_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["review_run_id"], ["review_runs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["symbol_id"], ["symbols.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["context_bundle_id"], ["context_bundles.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_review_candidates_review_run_id", "review_candidates", ["review_run_id"])

    op.create_table(
        "ai_finding_proposals",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("review_run_id", sa.Uuid(), nullable=False),
        sa.Column("candidate_id", sa.Uuid(), nullable=False),
        sa.Column("title", sa.String(length=512), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("category", _FINDING_CATEGORY, nullable=False),
        sa.Column("severity", _SEVERITY, nullable=False),
        sa.Column("confidence", _CONFIDENCE, nullable=False),
        sa.Column("file_path", sa.String(length=1024), nullable=False),
        sa.Column("start_line", sa.Integer(), nullable=False),
        sa.Column("end_line", sa.Integer(), nullable=False),
        sa.Column("evidence", sa.Text(), nullable=False),
        sa.Column("reasoning_summary", sa.Text(), nullable=False),
        sa.Column("suggested_fix", sa.Text(), nullable=True),
        sa.Column("status", _PROPOSAL_STATUS, nullable=False),
        sa.Column("validation_detail", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["review_run_id"], ["review_runs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["candidate_id"], ["review_candidates.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_ai_finding_proposals_review_run_id", "ai_finding_proposals", ["review_run_id"])
    op.create_index("ix_ai_finding_proposals_candidate_id", "ai_finding_proposals", ["candidate_id"])

    op.create_table(
        "critic_verdicts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("proposal_id", sa.Uuid(), nullable=False),
        sa.Column("decision", _CRITIC_DECISION, nullable=False),
        sa.Column("reasoning_summary", sa.Text(), nullable=False),
        sa.Column("downgraded_severity", _SEVERITY, nullable=True),
        sa.Column("downgraded_confidence", _CONFIDENCE, nullable=True),
        sa.Column("provider", sa.String(length=64), nullable=False),
        sa.Column("model", sa.String(length=128), nullable=False),
        sa.Column("input_tokens", sa.Integer(), nullable=False),
        sa.Column("output_tokens", sa.Integer(), nullable=False),
        sa.Column("latency_ms", sa.Float(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["proposal_id"], ["ai_finding_proposals.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_critic_verdicts_proposal_id", "critic_verdicts", ["proposal_id"], unique=True)

    op.create_table(
        "ai_findings",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("review_run_id", sa.Uuid(), nullable=False),
        sa.Column("proposal_id", sa.Uuid(), nullable=False),
        sa.Column("candidate_id", sa.Uuid(), nullable=False),
        sa.Column("title", sa.String(length=512), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("category", _FINDING_CATEGORY, nullable=False),
        sa.Column("severity", _SEVERITY, nullable=False),
        sa.Column("confidence", _CONFIDENCE, nullable=False),
        sa.Column("file_path", sa.String(length=1024), nullable=False),
        sa.Column("start_line", sa.Integer(), nullable=False),
        sa.Column("end_line", sa.Integer(), nullable=False),
        sa.Column("evidence", sa.Text(), nullable=False),
        sa.Column("reasoning_summary", sa.Text(), nullable=False),
        sa.Column("suggested_fix", sa.Text(), nullable=True),
        sa.Column("corroborated_by_static", sa.Boolean(), nullable=False),
        sa.Column("static_finding_ids", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["review_run_id"], ["review_runs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["proposal_id"], ["ai_finding_proposals.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["candidate_id"], ["review_candidates.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_ai_findings_review_run_id", "ai_findings", ["review_run_id"])
    op.create_index("ix_ai_findings_proposal_id", "ai_findings", ["proposal_id"], unique=True)
    op.create_index("ix_ai_findings_file_path", "ai_findings", ["review_run_id", "file_path"])
    op.create_index("ix_ai_findings_severity", "ai_findings", ["review_run_id", "severity"])


def downgrade() -> None:
    op.drop_index("ix_ai_findings_severity", table_name="ai_findings")
    op.drop_index("ix_ai_findings_file_path", table_name="ai_findings")
    op.drop_index("ix_ai_findings_proposal_id", table_name="ai_findings")
    op.drop_index("ix_ai_findings_review_run_id", table_name="ai_findings")
    op.drop_table("ai_findings")

    op.drop_index("ix_critic_verdicts_proposal_id", table_name="critic_verdicts")
    op.drop_table("critic_verdicts")

    op.drop_index("ix_ai_finding_proposals_candidate_id", table_name="ai_finding_proposals")
    op.drop_index("ix_ai_finding_proposals_review_run_id", table_name="ai_finding_proposals")
    op.drop_table("ai_finding_proposals")

    op.drop_index("ix_review_candidates_review_run_id", table_name="review_candidates")
    op.drop_table("review_candidates")

    op.drop_index("uq_review_runs_succeeded_identity", table_name="review_runs")
    op.drop_index("ix_review_runs_pull_request_id", table_name="review_runs")
    op.drop_index("ix_review_runs_repository_index_id", table_name="review_runs")
    op.drop_index("ix_review_runs_repository_id", table_name="review_runs")
    op.drop_table("review_runs")
