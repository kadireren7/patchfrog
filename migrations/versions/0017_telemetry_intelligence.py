"""Add reviewer provider-work latency aggregate for telemetry

Revision ID: 0017_telemetry_intelligence
Revises: 0016_quality_cost_guard
Create Date: 2026-09-01

"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0017_telemetry_intelligence"
down_revision: str | None = "0016_quality_cost_guard"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    # The two genuinely missing source-of-truth columns this milestone
    # needs (see docs/telemetry-intelligence.md's "persistence strategy"
    # section) -- everything else telemetry reports is derived from data
    # already persisted by earlier milestones.
    #
    # 1. A run-level sum of every specialist role call's provider-
    #    reported latency, never captured anywhere before now. Critic
    #    latency is already reconstructable per-verdict from
    #    ``critic_verdicts.latency_ms`` (added in migration 0006), so no
    #    second column is needed for that side.
    op.add_column(
        "review_runs", sa.Column("reviewer_latency_ms", sa.Float(), nullable=False, server_default="0.0")
    )
    # 2. The typed ValidationOutcome each proposal's deterministic
    #    validation actually produced. Before this column, only free-text
    #    ``validation_detail`` prose was persisted -- telemetry must never
    #    infer a machine-classified outcome from prose (spec section 5),
    #    so the discriminated enum value itself must be stored. Nullable:
    #    rows persisted before this column existed read back as
    #    "unknown", never a fabricated outcome.
    op.add_column(
        "ai_finding_proposals", sa.Column("validation_outcome", sa.String(length=32), nullable=True)
    )
    # 3. Per-specialist-role reviewer call counts -- previously computed
    #    in-memory only (ReviewRunSummary.calls_by_role) and never
    #    persisted, so a reused/reconstructed run summary always lost it.
    #    Per-role token counts alone cannot tell "one large call" apart
    #    from "several small calls" (spec section 10).
    op.add_column(
        "review_runs", sa.Column("calls_by_role", sa.Text(), nullable=False, server_default="{}")
    )


def downgrade() -> None:
    op.drop_column("review_runs", "calls_by_role")
    op.drop_column("ai_finding_proposals", "validation_outcome")
    op.drop_column("review_runs", "reviewer_latency_ms")
