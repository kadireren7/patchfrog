"""Internal domain models for PR-scoped incremental review memory (Phase 7).

Mirrors :mod:`patchfrog.review.domain`/:mod:`patchfrog.publishing.domain`'s
role for their engines: the stable, pure boundary everything in
:mod:`patchfrog.review_memory` shares. Reuses
:class:`patchfrog.analysis.domain.FindingCategory`/``Severity``,
:class:`patchfrog.domain.code.Language`/``SymbolKind``, and
:class:`patchfrog.indexing.models.ChangeSet` rather than inventing
parallel vocabularies.

Core principle (see the module docstring of
:mod:`patchfrog.review_memory.service`): review memory is advisory state
derived from exact commit history, never trusted over the current
repository state. Every ambiguous case resolves to "no reuse, fresh
review" -- these types make that ambiguity explicit and inspectable
(``verified``/``AMBIGUOUS`` statuses, explicit reason codes) rather than
folding it into a bare boolean anywhere.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from uuid import UUID

from patchfrog.analysis.domain import FindingCategory, Severity
from patchfrog.domain.code import Language, SymbolKind
from patchfrog.indexing.models import ChangeSet
from patchfrog.review.domain import ReviewCandidate


class IncrementalModePolicy(StrEnum):
    """Configured policy (``.patchfrog.yml`` ``review.incremental``)."""

    OFF = "off"
    AUTO = "auto"
    FORCE_INCREMENTAL = "force_incremental"


class IncrementalRunMode(StrEnum):
    """What one specific review run actually did -- part of that run's
    canonical identity (see :mod:`patchfrog.review_memory.config`'s
    ``compute_incremental_context_fingerprint``)."""

    FULL = "full"
    INCREMENTAL = "incremental"


class FindingMemoryStatus(StrEnum):
    """Lifecycle status of one :class:`ReviewMemoryFinding`. Never a bare
    boolean -- every status change carries an explicit
    :class:`TransitionReasonCode`."""

    OPEN = "open"
    CARRIED_FORWARD = "carried_forward"
    CHANGED = "changed"
    RESOLVED = "resolved"
    SUPERSEDED = "superseded"
    AMBIGUOUS = "ambiguous"


class TransitionReasonCode(StrEnum):
    """Why a memory finding (or a whole review generation) transitioned
    to a given status. Always persisted alongside the transition -- see
    :mod:`patchfrog.persistence.models.review_memory`."""

    SYMBOL_UNCHANGED = "symbol_unchanged"
    LINE_ONLY_MOVED = "line_only_moved"
    FILE_RENAMED = "file_renamed"
    SYMBOL_MODIFIED = "symbol_modified"
    EVIDENCE_REGION_CHANGED = "evidence_region_changed"
    #: A finding was carried forward with **zero** reviewer/critic
    #: provider calls this run: symbol continuity was UNCHANGED/MOVED/
    #: RENAMED *and* the finding's stored evidence snippet(s) were
    #: independently reconfirmed verbatim (whitespace-normalized, never
    #: fuzzy/LLM-matched) against the exact current commit's file
    #: content. See :mod:`patchfrog.review_memory.evidence`. Always
    #: persisted explicitly -- never folded into ``SYMBOL_UNCHANGED``/
    #: ``LINE_ONLY_MOVED``/``FILE_RENAMED``, which describe *symbol*
    #: continuity, not the evidence-based carry-forward decision itself.
    EVIDENCE_CONFIRMED_UNCHANGED = "evidence_confirmed_unchanged"
    SYMBOL_DELETED = "symbol_deleted"
    FILE_DELETED = "file_deleted"
    HISTORY_REWRITTEN = "history_rewritten"
    AMBIGUOUS_SYMBOL_MATCH = "ambiguous_symbol_match"
    PREVIOUS_FINDING_MISSING = "previous_finding_missing"
    NEW_FINDING = "new_finding"
    BASE_CHANGED = "base_changed"
    TOOLCHAIN_DRIFT = "toolchain_drift"
    MODEL_DRIFT = "model_drift"
    NO_PREVIOUS_REVIEW = "no_previous_review"
    PARTIAL_PREVIOUS_REVIEW = "partial_previous_review"
    RECHECK_CONFIRMED = "recheck_confirmed"
    RECHECK_NO_LONGER_PRESENT = "recheck_no_longer_present"
    ANCESTRY_UNVERIFIABLE = "ancestry_unverifiable"


class SymbolContinuityStatus(StrEnum):
    UNCHANGED = "unchanged"
    MODIFIED = "modified"
    ADDED = "added"
    DELETED = "deleted"
    MOVED = "moved"
    RENAMED = "renamed"
    AMBIGUOUS = "ambiguous"


@dataclass(frozen=True, slots=True)
class EvidenceSnippet:
    """One verbatim excerpt a finding's evidence rested on -- the
    deterministic identity :mod:`patchfrog.review_memory.evidence`
    revalidates against the exact current commit's real file content.
    Mirrors :class:`patchfrog.review.domain.ReviewEvidence`/the JSON shape
    already persisted on :class:`~patchfrog.persistence.models.review.AIFindingModel.evidence`
    -- deliberately not re-derived or reused from a
    :class:`~patchfrog.context.domain.ContextBundle`, which can go stale
    relative to the exact current commit (see the module docstring of
    :mod:`patchfrog.review_memory.evidence`)."""

    file_path: str
    start_line: int
    end_line: int
    quoted_text: str


def serialize_evidence(evidence: Sequence[EvidenceSnippet]) -> str:
    """The one JSON shape both :class:`~patchfrog.persistence.models.review.AIFindingModel.evidence`
    and :class:`~patchfrog.persistence.models.review_memory.ReviewMemoryFindingModel.evidence`
    use -- kept as a single shared pure function so the two never drift
    into subtly incompatible encodings."""

    return json.dumps(
        [
            {"file_path": e.file_path, "start_line": e.start_line, "end_line": e.end_line, "quoted_text": e.quoted_text}
            for e in evidence
        ]
    )


def parse_evidence_json(raw: str) -> tuple[EvidenceSnippet, ...]:
    """Inverse of :func:`serialize_evidence`. Never raises -- malformed
    or unexpected content (e.g. a pre-Phase-7.1 row, or genuinely
    corrupt data) deserializes to an empty tuple, which
    :mod:`patchfrog.review_memory.evidence` already treats as "cannot
    confirm, needs recheck" -- fails closed, never crashes a read path."""

    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return ()
    if not isinstance(parsed, list):
        return ()
    result: list[EvidenceSnippet] = []
    for entry in parsed:
        if not isinstance(entry, dict):
            continue
        try:
            result.append(
                EvidenceSnippet(
                    file_path=str(entry["file_path"]),
                    start_line=int(entry["start_line"]),
                    end_line=int(entry["end_line"]),
                    quoted_text=str(entry["quoted_text"]),
                )
            )
        except (KeyError, TypeError, ValueError):
            continue
    return tuple(result)


@dataclass(frozen=True, slots=True)
class SymbolSnapshot:
    """A plain, persistence-independent projection of one
    :class:`~patchfrog.persistence.models.code_index.SymbolModel` row
    (joined with its file's path) -- keeps
    :mod:`patchfrog.review_memory.symbol_continuity` free of any
    SQLAlchemy/session dependency, so it is fully unit-testable."""

    id: UUID
    name: str
    qualified_name: str
    kind: SymbolKind
    language: Language
    file_path: str
    start_line: int
    end_line: int
    content_hash: str


@dataclass(frozen=True, slots=True)
class SymbolContinuityResult:
    """One previous-and/or-current symbol's continuity classification.
    Exactly one of ``previous``/``current`` is ``None`` for ``ADDED``/
    ``DELETED``; both are set otherwise."""

    status: SymbolContinuityStatus
    previous: SymbolSnapshot | None
    current: SymbolSnapshot | None
    reason: str


@dataclass(frozen=True, slots=True)
class IncrementalChangeSet:
    """The deterministic delta between the previous usable review's
    commit and the current one -- file-level (reusing Phase 2's own
    :class:`~patchfrog.indexing.models.ChangeSet`, never duplicated) plus
    symbol-level continuity classification."""

    previous_commit_sha: str
    current_commit_sha: str
    file_changes: ChangeSet
    symbol_changes: tuple[SymbolContinuityResult, ...] = field(default_factory=tuple)

    @property
    def changed_symbol_ids(self) -> frozenset[UUID]:
        """Current-side symbol ids whose code actually changed (or whose
        continuity could not be established with confidence) -- these,
        and only these, ever need a fresh AI look under incremental mode."""

        return frozenset(
            r.current.id
            for r in self.symbol_changes
            if r.current is not None
            and r.status
            in (SymbolContinuityStatus.MODIFIED, SymbolContinuityStatus.ADDED, SymbolContinuityStatus.AMBIGUOUS)
        )


@dataclass(frozen=True, slots=True)
class ReviewMemoryFinding:
    """A durable, PR-scoped memory of one previously-accepted final
    finding (a Phase 5 :class:`~patchfrog.persistence.models.review.AIFindingModel`
    row) -- exists independently of whether it was ever published (see
    the module docstring of :mod:`patchfrog.review_memory.service` on the
    accepted-vs-published distinction).
    """

    id: UUID
    source_review_run_id: UUID
    source_finding_id: UUID
    repository_id: UUID
    pull_request_id: UUID
    first_seen_commit_sha: str
    last_seen_commit_sha: str
    file_path: str
    symbol_id: UUID | None
    symbol_qualified_name: str | None
    symbol_kind: SymbolKind | None
    category: FindingCategory
    severity: Severity
    title: str
    message: str
    start_line: int
    end_line: int
    exact_fingerprint: str
    semantic_family_fingerprint: str
    status: FindingMemoryStatus
    #: Deterministic evidence identity, persisted at creation time and
    #: refreshed only when a fresh AI look reconfirms this finding (see
    #: ``MemoryFindingDecision.updated_evidence``) -- never refreshed on
    #: a zero-AI-call carry-forward, since UNCHANGED/MOVED/RENAMED
    #: symbol continuity already guarantees byte-identical body content,
    #: so the original evidence text remains exactly as valid to check
    #: against. Empty for any pre-Phase-7.1 row (backfilled), which
    #: correctly fails closed to "cannot confirm, needs recheck" in
    #: :mod:`patchfrog.review_memory.evidence`.
    evidence: tuple[EvidenceSnippet, ...] = field(default_factory=tuple)


class PreReviewDisposition(StrEnum):
    """What :class:`patchfrog.review_memory.resolver.ReviewMemoryResolver`
    decided a previous finding needs *before* any AI call -- the input to
    :class:`patchfrog.review_memory.planner.IncrementalReviewPlanner`'s
    candidate selection."""

    CARRY_FORWARD = "carry_forward"
    NEEDS_RECHECK = "needs_recheck"
    RESOLVED_IMMEDIATELY = "resolved_immediately"


@dataclass(frozen=True, slots=True)
class PreReviewDecision:
    """Pre-AI-review disposition for one previous finding. Symbol/file
    deletion resolves immediately (nothing left to recheck); an unchanged
    or purely-moved symbol carries forward without spending a provider
    call; everything else (modified, ambiguous, evidence region changed)
    needs a fresh AI look before its final status is known -- see
    :meth:`patchfrog.review_memory.resolver.ReviewMemoryResolver.reconcile_post_review`.
    """

    memory_finding: ReviewMemoryFinding
    disposition: PreReviewDisposition
    reason: TransitionReasonCode
    detail: str
    updated_symbol_id: UUID | None = None
    updated_file_path: str | None = None
    updated_start_line: int | None = None
    updated_end_line: int | None = None


@dataclass(frozen=True, slots=True)
class CurrentFinding:
    """A minimal, review_memory-owned projection of one just-persisted
    :class:`~patchfrog.persistence.models.review.AIFindingModel` row
    (joined with its :class:`~patchfrog.persistence.models.review.ReviewCandidateModel`
    for ``symbol_id``) -- decouples
    :meth:`patchfrog.review_memory.resolver.ReviewMemoryResolver.reconcile_post_review`
    from Phase 5's own in-memory-only :class:`patchfrog.review.domain.FinalAIFinding`
    shape, which no longer exists once a run has completed and been
    persisted -- reconciliation runs strictly after persistence, from a
    fresh query, never from Phase 5's in-process state."""

    id: UUID
    symbol_id: UUID | None
    symbol_qualified_name: str | None
    symbol_kind: SymbolKind | None
    file_path: str
    category: FindingCategory
    severity: Severity
    title: str
    message: str
    start_line: int
    end_line: int
    evidence: tuple[EvidenceSnippet, ...] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class MemoryFindingDecision:
    """The resolver's decision for one previous :class:`ReviewMemoryFinding`
    against the current commit -- always carries a reason, never a bare
    status flip."""

    memory_finding: ReviewMemoryFinding
    new_status: FindingMemoryStatus
    reason: TransitionReasonCode
    detail: str
    updated_file_path: str | None = None
    updated_start_line: int | None = None
    updated_end_line: int | None = None
    updated_symbol_id: UUID | None = None
    #: The new run's :class:`CurrentFinding` id this decision matched
    #: against, when a fresh AI look actually produced one (RECHECK_CONFIRMED
    #: carry-forward or CHANGED) -- ``None`` for a pure skip-carry-forward
    #: (no recheck happened, so no new finding row exists for this run) and
    #: for RESOLVED/SUPERSEDED. This is what
    #: :mod:`patchfrog.publishing.planner`'s already-reported suppression
    #: keys off of.
    updated_current_finding_id: UUID | None = None
    #: New evidence to persist on the memory finding, when a fresh AI
    #: look actually reconfirmed it this run (RECHECK_CONFIRMED) --
    #: ``None`` (leave the existing evidence untouched) for a pure
    #: zero-AI-call carry-forward, where the underlying body is
    #: byte-identical by construction (see ``ReviewMemoryFinding.evidence``'s
    #: docstring for why refreshing would be redundant there) and for
    #: RESOLVED/SUPERSEDED, where evidence no longer matters.
    updated_evidence: tuple[EvidenceSnippet, ...] | None = None


@dataclass(frozen=True, slots=True)
class PreviousGenerationCandidate:
    """One previously-succeeded review generation for this PR, before
    ancestry/compatibility have been checked against the current commit."""

    generation_id: UUID
    review_run_id: UUID
    commit_sha: str
    repository_index_id: UUID
    memory_compatibility_fingerprint: str


@dataclass(frozen=True, slots=True)
class PreviousReviewSelection:
    """The outcome of looking for a usable previous review for the
    current (repository_id, pull_request_id, current_commit_sha).

    Two independent usability flags, deliberately not one: ancestry
    failure invalidates *everything* (no memory at all), but a
    model/prompt/toolchain compatibility mismatch ("drift") only
    disables *candidate skipping* -- finding-lifecycle memory (carry-
    forward/resolved detection, publication suppression) is still valid
    and retained for historical comparison. See the "Model/prompt
    drift" and "Static analyzer drift" sections of the Phase 7 spec.
    """

    candidate: PreviousGenerationCandidate | None
    ancestry_verified: bool
    compatibility_ok: bool
    usable_for_candidate_skipping: bool
    usable_for_finding_memory: bool
    invalidation_reason: TransitionReasonCode | None
    detail: str


@dataclass(frozen=True, slots=True)
class IncrementalMetrics:
    previous_candidate_count: int
    current_full_candidate_count: int
    incremental_candidate_count: int
    #: Total candidates this run never sent to the reviewer at all --
    #: every skipped candidate avoided exactly one reviewer call (and,
    #: if it would have produced a proposal, a critic call too). Equal
    #: to ``candidates_skipped_finding_free + candidates_skipped_evidence_confirmed``.
    candidates_skipped_by_memory: int
    #: Same count as ``candidates_skipped_by_memory``, named explicitly
    #: for "prove zero provider calls happened" reporting -- kept as a
    #: distinct, explicitly-named field rather than only an implicit
    #: rename so a metrics consumer never has to know the two are
    #: definitionally identical to find it.
    provider_calls_avoided: int
    #: Of ``candidates_skipped_by_memory``: skipped because the symbol
    #: was untouched *and* had no open memory finding attached at all --
    #: the original, always-safe incremental-savings mechanism.
    candidates_skipped_finding_free: int
    #: Of ``candidates_skipped_by_memory``: skipped because a *tracked*
    #: open finding's symbol was untouched (UNCHANGED/MOVED/RENAMED) and
    #: its stored evidence was independently, deterministically
    #: reconfirmed against the exact current commit -- the case this
    #: phase's evidence-revalidation work makes newly reachable. See
    #: ``TransitionReasonCode.EVIDENCE_CONFIRMED_UNCHANGED``.
    candidates_skipped_evidence_confirmed: int
    findings_carried_forward: int
    findings_changed: int
    findings_resolved: int
    findings_new: int
    findings_ambiguous: int


@dataclass(frozen=True, slots=True)
class IncrementalPlan:
    """Pure output of :class:`patchfrog.review_memory.planner.IncrementalReviewPlanner`
    -- everything needed to run (or skip) AI review for each candidate,
    with no network/LLM call involved in producing it."""

    mode: IncrementalRunMode
    selection: PreviousReviewSelection
    selected_candidates: tuple[ReviewCandidate, ...]
    skipped_candidates: tuple[ReviewCandidate, ...]
    pre_review_decisions: tuple[PreReviewDecision, ...]
    metrics: IncrementalMetrics


@dataclass(frozen=True, slots=True)
class FindingReconciliation:
    """After a (full or incremental) AI review run completes, how its
    *current* final findings relate to PR memory -- the transitions
    persisted as :class:`~patchfrog.persistence.models.review_memory.ReviewMemoryTransitionModel`
    rows."""

    new_finding_ids: frozenset[UUID]
    resolved_memory_finding_ids: frozenset[UUID]
    changed_pairs: tuple[tuple[UUID, UUID], ...] = field(default_factory=tuple)  # (memory_finding_id, new_finding_id)
