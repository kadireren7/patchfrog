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
    """The final, ranked, bounded context for one target."""

    target: ContextTarget
    items: tuple[ContextItem, ...]
    total_tokens_estimate: int
    total_lines: int
    config_fingerprint: str
    generation_version: int
    metrics: ContextQualityMetrics
    reused_existing_bundle: bool = field(default=False)
