"""Internal domain models for the deterministic Context Engine.

Mirrors :mod:`patchfrog.analysis.domain`'s role for the static analysis
engine: the stable boundary every context-engine component (candidate
generation, scoring, budgeting, dedup, persistence) shares. No LLM,
embedding, or natural-language concept belongs here or anywhere in this
package -- context selection is entirely structural, driven by Phase 2's
repository graph and Phase 3's finding/changed-line metadata.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Literal
from uuid import UUID


class ContextTargetType(StrEnum):
    """What kind of thing a context bundle was built for."""

    FINDING = "finding"
    SYMBOL = "symbol"
    LINE = "line"


@dataclass(frozen=True, slots=True)
class ContextTarget:
    """Explicit, self-contained description of what to build context for.

    Never relies on implicit global/session state -- every field needed
    to reproduce the exact same target is present here. ``commit_sha``
    and ``repository_index_id`` must agree (see
    :class:`~patchfrog.context.service.StaleContextIndexError`); building
    context against a repository index from a different commit is never
    permitted.
    """

    repository_id: UUID
    repository_index_id: UUID
    commit_sha: str
    target_type: ContextTargetType
    file_path: str
    line: int | None = None
    symbol_id: UUID | None = None
    finding_id: UUID | None = None
    analysis_run_id: UUID | None = None

    def fingerprint(self) -> str:
        """A stable identity for *what* this target is -- independent of
        ``repository_id``/``commit_sha``/``repository_index_id``, which
        the caller combines with this separately (see
        :meth:`patchfrog.context.config.ContextConfig.fingerprint` and
        the persisted bundle identity in
        :mod:`patchfrog.persistence.models.context`)."""

        parts = (
            self.target_type.value,
            self.file_path,
            str(self.line) if self.line is not None else "",
            str(self.symbol_id) if self.symbol_id is not None else "",
            str(self.finding_id) if self.finding_id is not None else "",
        )
        canonical = "\x1f".join(parts)
        return hashlib.sha256(canonical.encode()).hexdigest()


class ContextItemKind(StrEnum):
    """Only kinds actually backed by Phase 2 repository intelligence --
    never a fabricated or inferred semantic relationship."""

    TARGET_SYMBOL = "target_symbol"
    TARGET_FILE_REGION = "target_file_region"
    CALLER = "caller"
    CALLEE = "callee"
    IMPORTED_DEPENDENCY = "imported_dependency"
    INCLUDED_HEADER = "included_header"
    RELATED_TEST = "related_test"
    PARENT_SYMBOL = "parent_symbol"
    SIBLING_SYMBOL = "sibling_symbol"


class ContextRelationship(StrEnum):
    """Why an item was selected -- preserved on every item so a future
    (non-LLM, and eventually LLM) consumer can explain the bundle rather
    than receiving anonymous snippets."""

    TARGET_SYMBOL = "target_symbol"
    DIRECT_CALLER = "direct_caller"
    TRANSITIVE_CALLER = "transitive_caller"
    DIRECT_CALLEE = "direct_callee"
    TRANSITIVE_CALLEE = "transitive_callee"
    TESTS_TARGET_FILE = "tests_target_file"
    IMPORT_DEPENDENCY = "import_dependency"
    INCLUDE_DEPENDENCY = "include_dependency"
    PARENT_SYMBOL = "parent_symbol"
    SIBLING_SYMBOL = "sibling_symbol"


@dataclass(frozen=True, slots=True)
class ScoreComponent:
    """One named contribution to an item's final score -- see
    :mod:`patchfrog.context.scoring` for the full, explicit weight table."""

    label: str
    value: float


@dataclass(frozen=True, slots=True)
class ContextCandidate:
    """A candidate context item before scoring/budgeting/dedup -- the
    output of :mod:`patchfrog.context.candidates`."""

    kind: ContextItemKind
    file_path: str
    symbol_id: UUID | None
    symbol_name: str | None
    qualified_name: str | None
    start_line: int
    end_line: int
    relationship: ContextRelationship
    distance: int
    reason: str
    is_on_changed_line: bool = False
    #: The specific line that must survive trimming, if this candidate's
    #: span exceeds its budget cap -- e.g. the actual finding/target line,
    #: as opposed to just "the symbol's span starts here." ``None`` means
    #: no particular line matters more than any other (the default
    #: keep-the-start-of-the-range trimming is fine): callers/callees are
    #: shown as "the definition," not anchored to a specific point of
    #: interest within it.
    anchor_line: int | None = None


@dataclass(frozen=True, slots=True)
class ContextItem:
    """One selected, scored, snippet-populated piece of context."""

    kind: ContextItemKind
    file_path: str
    symbol_id: UUID | None
    symbol_name: str | None
    qualified_name: str | None
    start_line: int
    end_line: int
    content: str
    relationship: ContextRelationship
    distance: int
    score: float
    score_breakdown: tuple[ScoreComponent, ...]
    estimated_tokens: int
    reason: str
    truncated: bool = False


class ExpansionReason(StrEnum):
    """Why deterministic adaptive expansion (see
    :mod:`patchfrog.context.adaptive`) decided a candidate's depth-1
    context plausibly needs a second hop -- always a structural,
    pre-provider-call signal, never an LLM judgment or an NLP heuristic
    over comments/prose."""

    #: A. A depth-1 caller/callee itself has resolvable call edges
    #: outward -- a meaningful second hop exists to follow.
    CALL_CHAIN_CONTINUATION = "call_chain_continuation"
    #: B. A depth-1 caller/callee is itself on a changed line in this
    #: diff -- it's not just adjacent, it's part of what's actually
    #: being reviewed.
    CHANGED_NEIGHBOR = "changed_neighbor"
    #: C. The associated finding/candidate category is one where a
    #: direct call relationship is already known to matter (memory
    #: safety, resource management, concurrency, API misuse, security)
    #: -- see :mod:`patchfrog.context.scoring`'s existing category
    #: preference table, which this mirrors.
    STATIC_CATEGORY_RELEVANCE = "static_category_relevance"
    #: E. The target itself is structurally a thin wrapper/delegator --
    #: small span, exactly one direct callee -- so the *real* logic a
    #: reviewer needs is one hop further out.
    THIN_WRAPPER = "thin_wrapper"


#: Which direction(s) adaptive expansion selected -- deliberately a
#: closed, explicit set rather than a boolean pair, so "no signal
#: confidently pointed one way" (-> ``"both"``, bounded) is a distinct,
#: nameable outcome from "callers only" / "callees only".
ExpansionDirection = Literal["callers", "callees", "both"]


@dataclass(frozen=True, slots=True)
class ExpansionDecision:
    """The deterministic outcome of evaluating adaptive expansion
    signals for one target -- always produced, even when
    ``expand=False``, so "expansion was considered and declined" is
    distinguishable from "expansion was never attempted at all"
    (:attr:`AdaptiveContextMetrics.attempted`)."""

    expand: bool
    reasons: tuple[ExpansionReason, ...] = ()
    direction: ExpansionDirection | None = None


@dataclass(frozen=True, slots=True)
class AdaptiveContextMetrics:
    """Bundle-level adaptive provenance -- everything needed to answer
    "was expansion attempted, did it occur, why, and at what cost"
    without re-deriving it from raw item rows. ``None`` on
    :attr:`ContextBundle.adaptive_metrics` means adaptive mode was not
    requested for this bundle at all (fixed depth 1 or fixed depth 2)."""

    attempted: bool
    occurred: bool
    reasons: tuple[ExpansionReason, ...]
    direction: ExpansionDirection | None
    requested_max_depth: int
    effective_max_depth: int
    depth_2_candidate_count: int
    depth_2_selected_count: int
    depth_2_tokens: int


@dataclass(frozen=True, slots=True)
class ContextQualityMetrics:
    """Recorded for every generation run -- not exposed to end users, but
    useful for tuning ranking/budgeting without guessing."""

    candidate_count: int
    selected_count: int
    dropped_budget: int
    dropped_overlap: int
    dropped_duplicate: int
    total_tokens: int
    total_lines: int
    generation_ms: float


@dataclass(frozen=True, slots=True)
class ContextBundle:
    """The final, ranked, bounded context for one target.

    ``id`` is the persisted :class:`~patchfrog.persistence.models.context.ContextBundleModel`
    row's id -- always the *canonical* row for this bundle's identity,
    whether this call created it fresh or reused an existing succeeded
    bundle (including the narrow concurrent-write-loss case, where it is
    the winning concurrent bundle's id, not the id this call originally
    claimed). Deliberately exposed so callers (e.g. the AI Reviewer, which
    must record exactly which persisted context a provider request was
    built from) never have to guess or re-derive it.
    """

    id: UUID
    target: ContextTarget
    items: tuple[ContextItem, ...]
    total_tokens_estimate: int
    total_lines: int
    config_fingerprint: str
    generation_version: int
    metrics: ContextQualityMetrics
    reused_existing_bundle: bool = field(default=False)
    #: ``None`` unless adaptive expansion was requested for this bundle
    #: (see :mod:`patchfrog.context.adaptive`) -- fixed depth-1/depth-2
    #: bundles never have this populated.
    adaptive_metrics: AdaptiveContextMetrics | None = field(default=None)
