"""Analysis run identity: add toolchain_fingerprint

Canonical analysis-run reuse previously keyed only on
(repository_id, commit_sha, config_fingerprint) -- configuration *intent*,
never what was actually installed/discovered when the analyzers ran. That
let a stale succeeded run get reused as canonical after the effective
toolchain changed (e.g. the worker image upgraded ruff, or PatchFrog's own
bundled semgrep ruleset changed) even though the repository and
.patchfrog.yml were untouched -- analyzer behavior/findings can change
without either of those changing.

Adds toolchain_fingerprint (patchfrog.analysis.toolchain.ToolchainSnapshot
-- discovered analyzer versions, bundled ruleset content hash, analysis
engine version) as its own column, kept separate from config_fingerprint
by design, and folds it into the partial unique index so canonical reuse
now requires both to match.

Revision ID: 0004_toolchain_identity
Revises: 0003_static_analysis
Create Date: 2026-08-15

"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004_toolchain_identity"
down_revision: str | None = "0003_static_analysis"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

# Existing rows predate the concept of a discovered toolchain entirely --
# there is no "correct" historical value to backfill. Sentinel any
# pre-existing row instead of guessing so it can never collide with a real
# fingerprint and silently be treated as canonically reusable again by
# something that didn't intend that.
_LEGACY_SENTINEL = "0" * 64


def upgrade() -> None:
    op.add_column(
        "analysis_runs",
        sa.Column("toolchain_fingerprint", sa.String(length=64), nullable=True),
    )
    op.execute(
        sa.text("UPDATE analysis_runs SET toolchain_fingerprint = :sentinel").bindparams(
            sentinel=_LEGACY_SENTINEL
        )
    )
    with op.batch_alter_table("analysis_runs") as batch_op:
        batch_op.alter_column("toolchain_fingerprint", nullable=False)

    op.drop_index("uq_analysis_runs_succeeded_identity", table_name="analysis_runs")
    op.create_index(
        "uq_analysis_runs_succeeded_identity", "analysis_runs",
        ["repository_id", "commit_sha", "config_fingerprint", "toolchain_fingerprint"],
        unique=True,
        postgresql_where=sa.text("status = 'succeeded'"),
        sqlite_where=sa.text("status = 'succeeded'"),
    )


def downgrade() -> None:
    op.drop_index("uq_analysis_runs_succeeded_identity", table_name="analysis_runs")
    op.create_index(
        "uq_analysis_runs_succeeded_identity", "analysis_runs",
        ["repository_id", "commit_sha", "config_fingerprint"],
        unique=True,
        postgresql_where=sa.text("status = 'succeeded'"),
        sqlite_where=sa.text("status = 'succeeded'"),
    )
    with op.batch_alter_table("analysis_runs") as batch_op:
        batch_op.drop_column("toolchain_fingerprint")
