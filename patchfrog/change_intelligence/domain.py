"""Pure domain model for Change Intelligence -- no I/O, no LLM, no
database session (mirrors every other engine's own ``domain.py`` role,
e.g. :mod:`patchfrog.review.domain`, :mod:`patchfrog.context.domain`).

A :class:`ChangeUnit` represents one logical, behavioral change -- not
one file, not one diff hunk. Grouping (:mod:`patchfrog.change_intelligence.grouping`)
is entirely deterministic and graph-driven; nothing here is ever produced
by an LLM.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from patchfrog.review.domain import ReviewCandidate

#: Bumped whenever grouping/affected-surface/companion-heuristic/change-kind
#: logic changes materially enough that a prior report can no longer be
#: considered equivalent to what re-running now would produce. See
#: ``docs/change-intelligence.md`` for the full versioning discussion --
#: this is deliberately independent of REVIEW_ENGINE_VERSION/
#: REVIEW_POLICY_VERSION (those are about the AI reviewer's own call
#: shape/survival rules, not this deterministic, zero-LLM layer).
CHANGE_INTELLIGENCE_VERSION = 1

#: Hard bounds -- see ``docs/change-intelligence.md``'s "Bounds" section.
#: Never crawl the whole repository graph for one PR's affected surface.
MAX_AFFECTED_SURFACE_PER_UNIT = 25
MAX_GRAPH_DEPTH = 2
MAX_FANOUT_PER_SYMBOL = 50
MAX_CHANGE_MAP_NODES = 12
MAX_CHANGE_MAP_EDGES = 16


class ChangeKind(StrEnum):
    """A small, intentionally non-exhaustive, evidence-based taxonomy --
    see :mod:`patchfrog.change_intelligence.change_kind` for exactly what
    structural evidence backs each value. Never inferred from prose."""

    BEHAVIOR = "behavior"
    CONTRACT = "contract"
    PERSISTENCE = "persistence"
    CONFIGURATION = "configuration"
    TEST = "test"
    INFRASTRUCTURE = "infrastructure"
    MIXED = "mixed"


class AffectedRelation(StrEnum):
    """How one node in a :class:`ChangeUnit`'s affected surface relates
    to the change -- always derived from a real graph edge or the
    change itself, never guessed."""

    DIRECTLY_CHANGED = "directly_changed"
    DIRECTLY_DEPENDENT = "directly_dependent"
    INDIRECTLY_AFFECTED = "indirectly_affected"
    TEST = "test"


@dataclass(frozen=True, slots=True)
class AffectedSymbolRef:
    """One node in a :class:`ChangeUnit`'s affected surface. ``reason``
    is always a short, human-readable trace back to the exact graph
    evidence that included this node (spec section 5: "persist/telemeter
    enough to explain why a node was included")."""

    file_path: str
    qualified_name: str | None
    symbol_name: str | None
    relation: AffectedRelation
    distance: int
    reason: str


@dataclass(frozen=True, slots=True)
class ChangeUnit:
    """One logical, behavioral change -- a connected component of
    changed :class:`~patchfrog.review.domain.ReviewCandidate`\\ s over
    the existing repository graph, never a single file or a single diff
    hunk in isolation. ``id`` is a deterministic hash of its sorted
    member fingerprints, so the same input always produces the same id
    (stable across a retry, never random)."""

    id: str
    title: str
    change_kind: ChangeKind
    changed_candidates: tuple[ReviewCandidate, ...]
    affected_surface: tuple[AffectedSymbolRef, ...] = field(default_factory=tuple)

    @property
    def changed_files(self) -> tuple[str, ...]:
        seen: list[str] = []
        for c in self.changed_candidates:
            if c.file_path not in seen:
                seen.append(c.file_path)
        return tuple(seen)


class CompanionStatus(StrEnum):
    OBSERVED = "observed"
    MISSING = "missing"


class CompanionReasonCode(StrEnum):
    """Always exactly one of these -- see
    :mod:`patchfrog.change_intelligence.companions` for precisely which
    graph evidence produces each. Deliberately only two: every dependent-
    surface-not-updated example in the spec (serializer, loader,
    consumer, handler, negative test) reduces to one of these two
    *structural* signals at the evidence level this repository graph
    actually provides -- see ``docs/change-intelligence.md``."""

    CALLER_NOT_UPDATED = "caller_not_updated"
    TEST_NOT_UPDATED = "test_not_updated"


@dataclass(frozen=True, slots=True)
class ExpectedCompanionChange:
    """One candidate -- never a published finding on its own (spec
    section 7: "This layer produces CANDIDATES. Candidates must go
    through existing review/verifier machinery before publication.").
    ``status`` distinguishes an expected surface that *did* change
    (``OBSERVED`` -- recorded for completeness/telemetry, never
    surfaced as a "missing" candidate) from one that didn't
    (``MISSING`` -- the actionable case)."""

    change_unit_id: str
    source_qualified_name: str
    source_file_path: str
    expected_qualified_name: str
    expected_file_path: str
    reason_code: CompanionReasonCode
    reason: str
    evidence: str
    status: CompanionStatus


@dataclass(frozen=True, slots=True)
class AttentionArea:
    """One bounded, explainable "pay extra attention here" signal for a
    :class:`ChangeUnit` -- never a numeric score (spec section 12: "Do
    NOT add a numeric PR score")."""

    change_unit_id: str
    label: str
    signals: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ChangeMap:
    """A bounded, deterministic Markdown rendering -- see
    :mod:`patchfrog.change_intelligence.change_map`. ``text`` never
    exceeds the size bounds documented there; ``truncated`` is set
    (never silently) when real edges/nodes had to be omitted."""

    text: str
    node_count: int
    edge_count: int
    truncated: bool


@dataclass(frozen=True, slots=True)
class ChangeIntelligenceReport:
    """The complete, deterministic output for one review run. Never
    itself sent to an LLM in bulk -- only small, bounded per-candidate
    slices are (see :mod:`patchfrog.change_intelligence.service`'s
    ``evidence_text_for_candidate``)."""

    version: int
    change_units: tuple[ChangeUnit, ...]
    expected_companions: tuple[ExpectedCompanionChange, ...]
    attention_areas: tuple[AttentionArea, ...]
    change_story: str
    change_map: ChangeMap | None

    @property
    def missing_companion_candidates(self) -> tuple[ExpectedCompanionChange, ...]:
        return tuple(c for c in self.expected_companions if c.status is CompanionStatus.MISSING)

    @property
    def affected_surface_count(self) -> int:
        return sum(len(u.affected_surface) for u in self.change_units)
