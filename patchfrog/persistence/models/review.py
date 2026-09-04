"""Persisted AI Reviewer tables for one review run.

Mirrors :mod:`patchfrog.persistence.models.analysis` and
:mod:`patchfrog.persistence.models.context`'s shape exactly: a canonical
run row (``review_runs``) plus child tables cascade-deleted with it.
Canonical-run identity is ``(repository_id, commit_sha, config_fingerprint,
model_fingerprint)`` -- ``model_fingerprint`` folds in the *effective*
reviewer/critic provider+model plus PatchFrog's own prompt/policy/engine
versions (see :class:`patchfrog.review.config.ReviewModelIdentity`), so a
model swap, a provider swap, or a prompt/policy change each invalidate
reuse of a prior canonical run -- the same toolchain-awareness fix Phase 3
required for the static analysis engine.

``ai_finding_proposals`` is the full audit trail: every proposal the
reviewer made is persisted regardless of outcome, including rejected and
suppressed ones, with the reason recorded on ``status``/``validation_detail``.
``ai_findings`` holds only the subset that survived validation, the
critic, confidence aggregation, and dedup -- the only table a
user-facing query should ever read from.

Both tables also carry ``agent_role`` (see
:mod:`patchfrog.review.agents.roles`, Agent Orchestration v1): the
specialist that produced a given proposal/finding, nullable for rows
persisted before that milestone existed. ``review_runs`` similarly
carries a per-role token-usage breakdown alongside the pre-existing
``reviewer_input_tokens``/``reviewer_output_tokens`` totals, whose
meaning is unchanged.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import DateTime, Float, ForeignKey, Index, Integer, String, Text, func, text
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import Boolean, Uuid

from patchfrog.analysis.domain import Confidence, FindingCategory, Severity
from patchfrog.persistence.models._enum import enum_column
from patchfrog.persistence.models.base import Base
from patchfrog.review.agents.roles import AgentRole
from patchfrog.review.domain import (
    CriticDecision,
    ProposalStatus,
    ReviewCandidateReason,
    ReviewRunStatus,
    ValidationOutcome,
)
from patchfrog.review.effort_types import ReviewEffortReason, ReviewEffortTier


class ReviewCandidateStatus(StrEnum):
    PENDING = "pending"
    REVIEWED = "reviewed"
    SKIPPED_BUDGET = "skipped_budget"
    FAILED = "failed"


class ReviewRunModel(Base):
    """One canonical AI review run for a repository at a specific commit."""

    __tablename__ = "review_runs"
    __table_args__ = (
        Index("ix_review_runs_repository_id", "repository_id"),
        Index("ix_review_runs_repository_index_id", "repository_index_id"),
        Index("ix_review_runs_pull_request_id", "pull_request_id"),
        Index(
            "uq_review_runs_succeeded_identity",
            "repository_id",
            "commit_sha",
            "config_fingerprint",
            "model_fingerprint",
            "incremental_context_fingerprint",
            unique=True,
            postgresql_where=text("status = 'succeeded'"),
            sqlite_where=text("status = 'succeeded'"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    repository_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("repositories.id"))
    repository_index_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("repository_indexes.id"))
    analysis_run_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("analysis_runs.id"), nullable=True
    )
    pull_request_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("pull_requests.id"), nullable=True
    )
    commit_sha: Mapped[str] = mapped_column(String(40))
    config_fingerprint: Mapped[str] = mapped_column(String(64))
    model_fingerprint: Mapped[str] = mapped_column(String(64))
    #: Folds in whether this run was FULL or INCREMENTAL and, for
    #: INCREMENTAL, exactly which previous review generation it is tied
    #: to -- see :func:`patchfrog.review_memory.config.compute_incremental_context_fingerprint`.
    #: A run that never went through Phase 7 orchestration at all (every
    #: pre-Phase-7 caller: CLI, tests, direct service use) gets the fixed
    #: ``NO_MEMORY_CONTEXT_FINGERPRINT`` default, so canonical-run reuse
    #: for the common "no incremental review" case is completely
    #: unaffected by this column's addition.
    incremental_context_fingerprint: Mapped[str] = mapped_column(String(64))
    status: Mapped[ReviewRunStatus] = mapped_column(enum_column(ReviewRunStatus, length=16))

    reviewer_provider: Mapped[str] = mapped_column(String(64))
    reviewer_model: Mapped[str] = mapped_column(String(128))
    critic_provider: Mapped[str | None] = mapped_column(String(64), nullable=True)
    critic_model: Mapped[str | None] = mapped_column(String(128), nullable=True)

    candidate_count: Mapped[int] = mapped_column(Integer, default=0)
    candidates_reviewed: Mapped[int] = mapped_column(Integer, default=0)
    candidates_failed: Mapped[int] = mapped_column(Integer, default=0)
    candidates_skipped_budget: Mapped[int] = mapped_column(Integer, default=0)
    proposals_count: Mapped[int] = mapped_column(Integer, default=0)
    accepted_count: Mapped[int] = mapped_column(Integer, default=0)
    rejected_count: Mapped[int] = mapped_column(Integer, default=0)
    suppressed_duplicate_count: Mapped[int] = mapped_column(Integer, default=0)

    reviewer_input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    reviewer_output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    critic_input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    critic_output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    #: Per-specialist-role breakdown of the totals above (see
    #: :mod:`patchfrog.review.orchestration`) -- ``reviewer_input_tokens``/
    #: ``reviewer_output_tokens`` remain the total across every role,
    #: unchanged in meaning from before Agent Orchestration existed.
    #: Nullable-safe defaults (0) for historical rows predating this
    #: column, which never ran cooperative orchestration at all.
    correctness_input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    correctness_output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    security_input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    security_output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    correctness_thinking_tokens: Mapped[int] = mapped_column(Integer, default=0)
    security_thinking_tokens: Mapped[int] = mapped_column(Integer, default=0)

    #: Quality + Cost Guard (:mod:`patchfrog.review.effort`) run-level
    #: aggregates. ``candidates_by_tier`` is a JSON object mapping tier
    #: value -> count (e.g. ``{"light": 3, "standard": 2}``), the same
    #: JSON-text-column pattern already used for
    #: ``ReviewCandidateModel.static_finding_ids``. All default to
    #: 0/``"{}"`` -- nullable-safe for historical rows that predate this
    #: milestone and never tiered anything.
    candidates_by_tier: Mapped[str] = mapped_column(Text, default="{}")
    candidates_escalated: Mapped[int] = mapped_column(Integer, default=0)
    critic_calls: Mapped[int] = mapped_column(Integer, default=0)
    retries_consumed: Mapped[int] = mapped_column(Integer, default=0)
    #: Per-specialist-role reviewer call counts (see
    #: :mod:`patchfrog.review.orchestration`) -- JSON object mapping role
    #: value -> call count, the same JSON-text-column pattern as
    #: ``candidates_by_tier``. Was previously computed in-memory
    #: (:attr:`patchfrog.review.domain.ReviewRunSummary.calls_by_role`)
    #: but never persisted -- a real gap for telemetry (spec section 10:
    #: "Reviewer: ... calls"), since per-role token *counts* alone
    #: (``correctness_input_tokens`` etc. above) cannot tell "one large
    #: call" apart from "several small calls." Default ``"{}"``:
    #: nullable-safe for historical rows.
    calls_by_role: Mapped[str] = mapped_column(Text, default="{}")
    #: Thinking/reasoning token totals (see
    #: :attr:`patchfrog.review.domain.TokenUsage.thinking_tokens`),
    #: broken out for providers/models that report them. 0 for a run
    #: where nothing reported them -- never fabricated.
    reviewer_thinking_tokens: Mapped[int] = mapped_column(Integer, default=0)
    critic_thinking_tokens: Mapped[int] = mapped_column(Integer, default=0)

    #: Sum of every specialist role call's provider-reported latency
    #: across the whole run (:mod:`patchfrog.telemetry`'s "provider-work
    #: latency aggregate") -- deliberately distinct from ``duration_ms``
    #: (wall clock) below: roles run concurrently and candidates may run
    #: concurrently too, so this can legitimately exceed ``duration_ms``.
    #: 0.0 default is nullable-safe for rows predating this milestone,
    #: which never captured per-role latency at all -- never fabricated.
    reviewer_latency_ms: Mapped[float] = mapped_column(Float, default=0.0)

    duration_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    #: Change Intelligence Foundation (:mod:`patchfrog.change_intelligence`)
    #: run-level summary -- the bounded output of
    #: :func:`patchfrog.change_intelligence.telemetry.summarize_for_persistence`,
    #: never the full report (no raw evidence text, no per-node reasoning
    #: beyond the already-bounded Change Story/Change Map text). All
    #: default to 0/``"{}"``/``False``/``None`` -- nullable-safe for
    #: historical rows predating this milestone, which never computed
    #: Change Intelligence at all.
    change_unit_count: Mapped[int] = mapped_column(Integer, default=0)
    change_kind_counts: Mapped[str] = mapped_column(Text, default="{}")
    affected_surface_count: Mapped[int] = mapped_column(Integer, default=0)
    expected_companion_count: Mapped[int] = mapped_column(Integer, default=0)
    missing_companion_candidate_count: Mapped[int] = mapped_column(Integer, default=0)
    change_map_rendered: Mapped[bool] = mapped_column(Boolean, default=False)
    change_map_node_count: Mapped[int] = mapped_column(Integer, default=0)
    change_story: Mapped[str | None] = mapped_column(Text, nullable=True)
    change_map_text: Mapped[str | None] = mapped_column(Text, nullable=True)

    #: Contract & Blast Radius Intelligence (:mod:`patchfrog.contract_intelligence`)
    #: run-level summary -- the bounded output of
    #: :func:`patchfrog.contract_intelligence.telemetry.summarize_for_persistence`.
    #: No separate contract-story/contract-map text columns: the Contract
    #: Story addendum is folded into ``change_story`` above, and the
    #: Contract Map reuses ``change_map_text``/``change_map_rendered``/
    #: ``change_map_node_count`` above (the *same* map, not a second one)
    #: -- see ``docs/contract-intelligence.md``'s Persistence section.
    #: All default to 0/``"{}"`` -- nullable-safe for rows predating this
    #: milestone.
    contract_delta_count: Mapped[int] = mapped_column(Integer, default=0)
    contract_kind_counts: Mapped[str] = mapped_column(Text, default="{}")
    potentially_breaking_delta_count: Mapped[int] = mapped_column(Integer, default=0)
    impacted_consumer_count: Mapped[int] = mapped_column(Integer, default=0)
    stale_consumer_candidate_count: Mapped[int] = mapped_column(Integer, default=0)

    #: Intent Verification Foundation (:mod:`patchfrog.intent_verification`)
    #: run-level summary. No separate intent-story text column: the
    #: Intent Story prefix is folded into ``change_story`` above (spec
    #: section 21). ``intent_coverage_summary_text`` IS a new, dedicated
    #: column -- unlike the Change/Contract Map, the conditional Intent
    #: Coverage block (spec section 22) is its own separate publication
    #: section, not a re-render of an existing one, so it needs its own
    #: bounded text for cross-task publication (same justification
    #: precedent as ``change_map_text`` -- publication is a separate,
    #: independently-retriable Celery task from review generation). All
    #: default to 0/``"{}"``/``False``/``None`` -- nullable-safe for rows
    #: predating this milestone.
    intent_claim_count: Mapped[int] = mapped_column(Integer, default=0)
    intent_source_kind_counts: Mapped[str] = mapped_column(Text, default="{}")
    mapped_intent_claim_count: Mapped[int] = mapped_column(Integer, default=0)
    intent_gap_candidate_count: Mapped[int] = mapped_column(Integer, default=0)
    intent_coverage_summary_rendered: Mapped[bool] = mapped_column(Boolean, default=False)
    intent_coverage_summary_text: Mapped[str | None] = mapped_column(Text, nullable=True)

    #: Test Intelligence Foundation (:mod:`patchfrog.test_intelligence`)
    #: run-level summary -- the bounded output of
    #: :func:`patchfrog.test_intelligence.telemetry.summarize_for_persistence`.
    #: No separate test-story text column: the Test Story prefix is
    #: folded into ``change_story`` above, exactly like the Intent Story
    #: prefix. ``test_coverage_summary_text`` IS a new, dedicated column
    #: -- the conditional Test Coverage block (mirrors the Intent
    #: Coverage block) is its own separate publication section, not a
    #: re-render of an existing one. All default to 0/``"{}"``/
    #: ``False``/``None`` -- nullable-safe for rows predating this
    #: milestone.
    test_expectation_count: Mapped[int] = mapped_column(Integer, default=0)
    test_reason_code_counts: Mapped[str] = mapped_column(Text, default="{}")
    test_gap_candidate_count: Mapped[int] = mapped_column(Integer, default=0)
    test_coverage_summary_rendered: Mapped[bool] = mapped_column(Boolean, default=False)
    test_coverage_summary_text: Mapped[str | None] = mapped_column(Text, nullable=True)


class ReviewCandidateModel(Base):
    """One symbol- (or module-region-) centered candidate considered for
    review within a run."""

    __tablename__ = "review_candidates"
    __table_args__ = (Index("ix_review_candidates_review_run_id", "review_run_id"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    review_run_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("review_runs.id", ondelete="CASCADE")
    )
    file_path: Mapped[str] = mapped_column(String(1024))
    symbol_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("symbols.id", ondelete="SET NULL"), nullable=True
    )
    symbol_name: Mapped[str | None] = mapped_column(String(512), nullable=True)
    qualified_name: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    start_line: Mapped[int] = mapped_column(Integer)
    end_line: Mapped[int] = mapped_column(Integer)
    changed_lines: Mapped[str] = mapped_column(Text)
    reason: Mapped[ReviewCandidateReason] = mapped_column(enum_column(ReviewCandidateReason, length=32))
    static_finding_ids: Mapped[str] = mapped_column(Text, default="[]")
    status: Mapped[ReviewCandidateStatus] = mapped_column(
        enum_column(ReviewCandidateStatus, length=16), default=ReviewCandidateStatus.PENDING
    )
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    context_bundle_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("context_bundles.id"), nullable=True
    )
    #: Quality + Cost Guard (:mod:`patchfrog.review.effort`) per-candidate
    #: audit fields. ``effort_tier``/``effort_reasons`` nullable: a
    #: candidate skipped for budget or one from a run that predates this
    #: milestone never had a tier decided at all. ``effort_reasons`` is a
    #: JSON array of reason values, mirroring ``static_finding_ids``'s
    #: JSON-text-column pattern.
    effort_tier: Mapped[ReviewEffortTier | None] = mapped_column(
        enum_column(ReviewEffortTier, length=16), nullable=True
    )
    effort_reasons: Mapped[str] = mapped_column(Text, default="[]")
    escalated: Mapped[bool] = mapped_column(Boolean, default=False)
    escalation_reason: Mapped[ReviewEffortReason | None] = mapped_column(
        enum_column(ReviewEffortReason, length=32), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AIFindingProposalModel(Base):
    """Full audit trail of every finding the reviewer proposed, whatever
    happened to it afterwards -- accepted, rejected by validation,
    rejected by the critic, rejected for low confidence, or suppressed as
    a duplicate. Never shown to users directly; see ``ai_findings``."""

    __tablename__ = "ai_finding_proposals"
    __table_args__ = (
        Index("ix_ai_finding_proposals_review_run_id", "review_run_id"),
        Index("ix_ai_finding_proposals_candidate_id", "candidate_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    review_run_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("review_runs.id", ondelete="CASCADE")
    )
    candidate_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("review_candidates.id", ondelete="CASCADE")
    )
    title: Mapped[str] = mapped_column(String(512))
    message: Mapped[str] = mapped_column(Text)
    category: Mapped[FindingCategory] = mapped_column(enum_column(FindingCategory, length=32))
    severity: Mapped[Severity] = mapped_column(enum_column(Severity, length=16))
    confidence: Mapped[Confidence] = mapped_column(enum_column(Confidence, length=16))
    file_path: Mapped[str] = mapped_column(String(1024))
    start_line: Mapped[int] = mapped_column(Integer)
    end_line: Mapped[int] = mapped_column(Integer)
    evidence: Mapped[str] = mapped_column(Text)
    reasoning_summary: Mapped[str] = mapped_column(Text)
    suggested_fix: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: Realistic, code-grounded consequence -- nullable: rows persisted
    #: before this column existed, and findings where impact genuinely
    #: cannot be established, both read back as ``None`` rather than a
    #: fabricated value (see migration 0011).
    impact: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[ProposalStatus] = mapped_column(enum_column(ProposalStatus, length=32))
    validation_detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: The typed :class:`~patchfrog.review.domain.ValidationOutcome` this
    #: proposal's own deterministic validation produced -- distinct from
    #: ``validation_detail`` (free-text prose, never machine-classified)
    #: and from ``status`` (the proposal's *overall* terminal disposition,
    #: which folds in critic/dedup/budget outcomes too). Populated for
    #: every proposal, not just rejected ones, since validation always
    #: runs and always produces one outcome regardless of what happens
    #: downstream. ``None`` only for rows persisted before this column
    #: existed -- never fabricated, never inferred from ``validation_detail``
    #: prose (see :mod:`patchfrog.telemetry`'s module docstring on why
    #: telemetry must never guess from free text).
    validation_outcome: Mapped[ValidationOutcome | None] = mapped_column(
        enum_column(ValidationOutcome, length=32), nullable=True
    )
    #: The specialist role (see :mod:`patchfrog.review.agents.roles`)
    #: that produced this proposal. Nullable: rows persisted before
    #: Agent Orchestration existed never had a role at all -- ``None``
    #: reads back honestly as "predates specialist attribution", never a
    #: fabricated role.
    agent_role: Mapped[AgentRole | None] = mapped_column(enum_column(AgentRole, length=32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class CriticVerdictModel(Base):
    """One critic verdict for one proposal -- at most one row per
    proposal (a proposal only ever reaches the critic once)."""

    __tablename__ = "critic_verdicts"
    __table_args__ = (Index("ix_critic_verdicts_proposal_id", "proposal_id", unique=True),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    proposal_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("ai_finding_proposals.id", ondelete="CASCADE")
    )
    decision: Mapped[CriticDecision] = mapped_column(enum_column(CriticDecision, length=16))
    reasoning_summary: Mapped[str] = mapped_column(Text)
    downgraded_severity: Mapped[Severity | None] = mapped_column(
        enum_column(Severity, length=16), nullable=True
    )
    downgraded_confidence: Mapped[Confidence | None] = mapped_column(
        enum_column(Confidence, length=16), nullable=True
    )
    provider: Mapped[str] = mapped_column(String(64))
    model: Mapped[str] = mapped_column(String(128))
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    #: See :attr:`patchfrog.review.domain.CriticVerdict.thinking_tokens`.
    thinking_tokens: Mapped[int] = mapped_column(Integer, default=0)
    latency_ms: Mapped[float] = mapped_column(Float, default=0.0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AIFindingModel(Base):
    """One AI finding that survived validation, the critic, confidence
    aggregation, and dedup -- the only table a query/presentation layer
    should ever read from."""

    __tablename__ = "ai_findings"
    __table_args__ = (
        Index("ix_ai_findings_review_run_id", "review_run_id"),
        Index("ix_ai_findings_proposal_id", "proposal_id", unique=True),
        Index("ix_ai_findings_file_path", "review_run_id", "file_path"),
        Index("ix_ai_findings_severity", "review_run_id", "severity"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    review_run_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("review_runs.id", ondelete="CASCADE")
    )
    proposal_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("ai_finding_proposals.id", ondelete="CASCADE")
    )
    candidate_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("review_candidates.id", ondelete="CASCADE")
    )
    title: Mapped[str] = mapped_column(String(512))
    message: Mapped[str] = mapped_column(Text)
    category: Mapped[FindingCategory] = mapped_column(enum_column(FindingCategory, length=32))
    severity: Mapped[Severity] = mapped_column(enum_column(Severity, length=16))
    confidence: Mapped[Confidence] = mapped_column(enum_column(Confidence, length=16))
    file_path: Mapped[str] = mapped_column(String(1024))
    start_line: Mapped[int] = mapped_column(Integer)
    end_line: Mapped[int] = mapped_column(Integer)
    evidence: Mapped[str] = mapped_column(Text)
    reasoning_summary: Mapped[str] = mapped_column(Text)
    suggested_fix: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: See :attr:`AIFindingProposalModel.impact`.
    impact: Mapped[str | None] = mapped_column(Text, nullable=True)
    corroborated_by_static: Mapped[bool] = mapped_column(Boolean, default=False)
    static_finding_ids: Mapped[str] = mapped_column(Text, default="[]")
    #: See :attr:`AIFindingProposalModel.agent_role`.
    agent_role: Mapped[AgentRole | None] = mapped_column(enum_column(AgentRole, length=32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
