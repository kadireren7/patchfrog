"""Internal domain models for the AI Reviewer.

Mirrors :mod:`patchfrog.analysis.domain` and :mod:`patchfrog.context.domain`'s
role for their respective engines: the stable boundary everything in this
package shares. Deliberately reuses
:class:`patchfrog.analysis.domain.FindingCategory`,
:class:`patchfrog.analysis.domain.Severity`, and
:class:`patchfrog.analysis.domain.Confidence` rather than inventing a
parallel taxonomy -- an AI finding and a static finding must be directly
comparable (for dedup, for display, for confidence aggregation) without a
translation layer between two incompatible vocabularies.

Nothing in this module ever crosses the provider boundary un-validated.
See :mod:`patchfrog.review.validation` -- a model's raw structured output
becomes an :class:`AIReviewFinding` only after evidence/location checks
pass, and a proposal only becomes a persisted, user-visible finding after
:mod:`patchfrog.review.critic` and :mod:`patchfrog.review.confidence` have
run. "The model may propose. PatchFrog decides what survives."
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from enum import StrEnum
from uuid import UUID

from patchfrog.analysis.domain import Confidence, FindingCategory, Severity


class ReviewCandidateReason(StrEnum):
    """Why a candidate was selected for AI review -- always traceable back
    to something structural (a changed line, a static finding), never an
    LLM judgment call about what's "interesting"."""

    CHANGED_SYMBOL = "changed_symbol"
    CHANGED_MODULE_REGION = "changed_module_region"
    STATIC_FINDING_EVIDENCE = "static_finding_evidence"


@dataclass(frozen=True, slots=True)
class ReviewCandidate:
    """One symbol- (or, absent a symbol, module-region-) centered unit of
    review, before any provider call is made.

    Candidate selection is deterministic and diff-driven: every candidate
    traces back to a changed line or a static finding on that commit --
    never to an LLM's own judgment about what looks interesting.
    """

    file_path: str
    symbol_id: UUID | None
    symbol_name: str | None
    qualified_name: str | None
    start_line: int
    end_line: int
    changed_lines: tuple[int, ...]
    static_finding_ids: tuple[UUID, ...]
    reason: ReviewCandidateReason

    def fingerprint(self) -> str:
        parts = (
            self.file_path,
            str(self.symbol_id) if self.symbol_id is not None else "",
            str(self.start_line),
            str(self.end_line),
        )
        canonical = "\x1f".join(parts)
        return hashlib.sha256(canonical.encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class StaticFindingSummary:
    """A compact, prompt-safe summary of one static finding attached to a
    candidate as *evidence*, never as ground truth the model must agree
    with (see the module docstring and :mod:`patchfrog.review.prompt`)."""

    finding_id: UUID
    rule_id: str
    category: FindingCategory
    severity: Severity
    confidence: Confidence
    title: str
    message: str
    start_line: int
    end_line: int
    source_analyzer: str


@dataclass(frozen=True, slots=True)
class ReviewEvidence:
    """A verbatim excerpt the model claims supports one of its findings --
    checked in :mod:`patchfrog.review.validation` against the exact
    context text that was actually sent, so a finding can never survive
    on evidence that was never shown to the model in the first place."""

    file_path: str
    start_line: int
    end_line: int
    quoted_text: str


@dataclass(frozen=True, slots=True)
class AIReviewFinding:
    """One structured finding as proposed by the reviewer model --
    pre-validation, pre-critic. Never shown to a user in this form.

    Five conceptually distinct pieces of analysis, kept as separate
    fields rather than folded into one prose blob so each can be
    independently validated, dedup'd, persisted, and rendered:

    - ``message``: identification -- the exact problematic condition
      (e.g. "`password` is interpolated into the returned error
      string"), not a vague restatement of the category.
    - ``reasoning_summary``: the technical mechanism/root cause (e.g.
      "the value reaches the response text without redaction").
    - ``impact``: realistic, code-grounded consequence. Nullable --
      when impact genuinely cannot be established from the given
      context, this stays ``None`` rather than a fabricated guess.
    - ``suggested_fix``: an actionable remediation direction, nullable.
    - ``severity``/``confidence``: unchanged from the pre-existing
      taxonomy (see the module docstring).

    Never a chain-of-thought transcript: ``reasoning_summary`` and
    ``impact`` are both scoped (see :mod:`patchfrog.review.prompt`) to a
    couple of final-answer sentences, never the model's internal
    deliberation.
    """

    title: str
    message: str
    category: FindingCategory
    severity: Severity
    confidence: Confidence
    file_path: str
    start_line: int
    end_line: int
    evidence: tuple[ReviewEvidence, ...]
    reasoning_summary: str
    suggested_fix: str | None = None
    impact: str | None = None


@dataclass(frozen=True, slots=True)
class ReviewRequest:
    """Everything sent to the provider for one candidate -- the complete,
    already-redacted payload. Kept around post-call for audit/replay."""

    candidate: ReviewCandidate
    context_bundle_id: UUID | None
    context_text: str
    static_findings: tuple[StaticFindingSummary, ...]
    diff_excerpt: str
    system_prompt: str
    user_prompt: str


@dataclass(frozen=True, slots=True)
class ReviewResponse:
    """The provider's structured response to one :class:`ReviewRequest`,
    still pre-validation."""

    findings: tuple[AIReviewFinding, ...]
    provider: str
    model: str
    input_tokens: int
    output_tokens: int
    latency_ms: float
    stop_reason: str | None = None


class ValidationOutcome(StrEnum):
    """The result of checking one :class:`AIReviewFinding` against the
    evidence/location rules in :mod:`patchfrog.review.validation`."""

    VALID = "valid"
    HALLUCINATED_LOCATION = "hallucinated_location"
    HALLUCINATED_EVIDENCE = "hallucinated_evidence"
    INVALID_TAXONOMY = "invalid_taxonomy"
    OUT_OF_SCOPE = "out_of_scope"
    #: ``message``/``reasoning_summary`` was empty -- a finding with no
    #: identification or no stated mechanism is never useful enough to
    #: reach the critic, regardless of how well-formed its other fields are.
    INCOMPLETE_ANALYSIS = "incomplete_analysis"


@dataclass(frozen=True, slots=True)
class ValidatedFinding:
    """An :class:`AIReviewFinding` after deterministic validation --
    ``outcome`` is always ``VALID`` for anything that proceeds to the
    critic; anything else is suppressed with a reason."""

    finding: AIReviewFinding
    outcome: ValidationOutcome
    detail: str


class CriticDecision(StrEnum):
    ACCEPT = "accept"
    REJECT = "reject"
    DOWNGRADE = "downgrade"


@dataclass(frozen=True, slots=True)
class CriticVerdict:
    """The second-stage LLM critic's structured verdict on one validated
    proposal. A ``DOWNGRADE`` always carries at least one of
    ``downgraded_severity``/``downgraded_confidence``."""

    decision: CriticDecision
    reasoning_summary: str
    downgraded_severity: Severity | None = None
    downgraded_confidence: Confidence | None = None
    provider: str = ""
    model: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: float = 0.0


class ProposalStatus(StrEnum):
    """Terminal disposition of one candidate's proposed finding, always
    persisted for audit regardless of outcome -- rejected proposals are
    never shown to users but are never silently discarded either."""

    ACCEPTED = "accepted"
    REJECTED_VALIDATION = "rejected_validation"
    REJECTED_CRITIC = "rejected_critic"
    REJECTED_LOW_CONFIDENCE = "rejected_low_confidence"
    SUPPRESSED_DUPLICATE = "suppressed_duplicate"


@dataclass(frozen=True, slots=True)
class FinalAIFinding:
    """One AI finding that survived validation, critic review, confidence
    aggregation, and dedup -- the only shape ever exposed to a consumer of
    :mod:`patchfrog.review.queries`.

    ``proposal_id``/``candidate_id`` are the already-persisted
    :class:`~patchfrog.persistence.models.review.AIFindingProposalModel`/
    :class:`~patchfrog.persistence.models.review.ReviewCandidateModel` rows
    this finding traces back to -- a ``FinalAIFinding`` is only ever
    constructed after its proposal row exists, since the full audit trail
    (see the module docstring of
    :mod:`patchfrog.persistence.models.review`) is written first.
    """

    proposal_id: UUID
    candidate_id: UUID
    candidate: ReviewCandidate
    finding: AIReviewFinding
    critic_verdict: CriticVerdict | None
    final_severity: Severity
    final_confidence: Confidence
    corroborated_by_static: bool
    static_finding_ids: tuple[UUID, ...]


class ReviewRunStatus(StrEnum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    PARTIAL = "partial"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class TokenUsage:
    input_tokens: int = 0
    output_tokens: int = 0

    def __add__(self, other: TokenUsage) -> TokenUsage:
        return TokenUsage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
        )


@dataclass(frozen=True, slots=True)
class ReviewRunSummary:
    """Metrics for one completed (or failed) AI review run."""

    run_id: UUID
    status: ReviewRunStatus
    candidate_count: int
    candidates_reviewed: int
    candidates_failed: int
    candidates_skipped_budget: int
    proposals_count: int
    accepted_count: int
    rejected_count: int
    suppressed_duplicate_count: int
    reviewer_usage: TokenUsage
    critic_usage: TokenUsage
    duration_ms: float
    reused_existing_run: bool = field(default=False)
