"""Pure domain model for Contract & Blast Radius Intelligence -- no I/O,
no LLM, no database session (mirrors :mod:`patchfrog.change_intelligence.domain`'s
own role).

Reuses :mod:`patchfrog.change_intelligence.domain` types directly where
the shape already fits (`AffectedSymbolRef` for blast radius,
`ExpectedCompanionChange`/`CompanionReasonCode.CONTRACT_CONSUMER_NOT_UPDATED`
for stale-consumer candidates) -- see this package's own docstring and
the audit in ``validation/contract_intelligence/latest-summary.md``
section 1 for why nothing here duplicates that package's graph/candidate
model.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from patchfrog.change_intelligence.domain import AffectedSymbolRef, ExpectedCompanionChange

#: Bumped whenever signature-parsing/delta/breaking-characteristic/
#: blast-radius/stale-consumer logic changes materially enough that a
#: prior report can no longer be considered equivalent to what
#: re-running now would produce. Independent of CHANGE_INTELLIGENCE_VERSION
#: (that package's own grouping/companion/affected-surface logic is
#: unchanged by this milestone -- see docs/contract-intelligence.md).
CONTRACT_INTELLIGENCE_VERSION = 1

#: A report considering more contract-eligible candidates than this is
#: already beyond what one PR review should ever produce -- same
#: defensive-bound rationale as
#: patchfrog.change_intelligence.service.MAX_CANDIDATES_CONSIDERED.
MAX_CANDIDATES_CONSIDERED = 150

#: Never fetch base-commit content for more files than this in one
#: review run, even if an unusually large PR touches many contract-
#: eligible functions across many files.
MAX_BASE_FILES_FETCHED = 40


class ContractKind(StrEnum):
    """A small, intentionally non-exhaustive taxonomy (spec section 2).

    Every value is kept here for forward extensibility and
    documentation, but **this milestone's detection logic only ever
    constructs `FUNCTION` descriptors** -- see
    ``validation/contract_intelligence/latest-summary.md`` section 1
    ("Which types would require guessing and must be deferred") for
    exactly why `SCHEMA`/`CONFIGURATION`/`PERSISTENCE`/`EVENT`/
    `PUBLIC_INTERFACE` are deferred rather than faked.
    """

    FUNCTION = "function"
    SCHEMA = "schema"
    CONFIGURATION = "configuration"
    PERSISTENCE = "persistence"
    EVENT = "event"
    PUBLIC_INTERFACE = "public_interface"


class BreakingCharacteristic(StrEnum):
    """A small set of deterministic, structural characteristics -- never
    a simplistic BREAKING/SAFE verdict, never a numeric compatibility
    score (spec section 6). See
    :func:`patchfrog.contract_intelligence.delta.diff_signatures` for
    exactly which structural evidence produces each value."""

    REQUIRED_PARAMETER_ADDED = "required_parameter_added"
    PARAMETER_REMOVED = "parameter_removed"
    DEFAULT_REMOVED = "default_removed"
    DEFAULT_ADDED = "default_added"
    OPTIONAL_PARAMETER_ADDED = "optional_parameter_added"
    RETURN_BECAME_OPTIONAL = "return_became_optional"
    RETURN_BECAME_REQUIRED = "return_became_required"
    RETURN_ANNOTATION_CHANGED = "return_annotation_changed"
    ASYNC_TO_SYNC = "async_to_sync"
    SYNC_TO_ASYNC = "sync_to_async"


#: The subset of `BreakingCharacteristic` that plausibly requires
#: consumer adaptation (spec section 8, requirement 4) -- these are the
#: only characteristics that ever trigger stale-consumer candidate
#: generation. The rest (`OPTIONAL_PARAMETER_ADDED`, `DEFAULT_ADDED`,
#: `RETURN_BECAME_REQUIRED`, `RETURN_ANNOTATION_CHANGED`) are recorded
#: as evidence on every `ContractDelta` but never treated as consumer-
#: breaking -- e.g. `RETURN_BECAME_REQUIRED`/`DEFAULT_ADDED` are
#: producer/backward-compatible-loosening concerns, not "an existing
#: unchanged caller may now be wrong" concerns.
BREAKING_CHARACTERISTICS: frozenset[BreakingCharacteristic] = frozenset(
    {
        BreakingCharacteristic.REQUIRED_PARAMETER_ADDED,
        BreakingCharacteristic.PARAMETER_REMOVED,
        BreakingCharacteristic.DEFAULT_REMOVED,
        BreakingCharacteristic.RETURN_BECAME_OPTIONAL,
        BreakingCharacteristic.ASYNC_TO_SYNC,
        BreakingCharacteristic.SYNC_TO_ASYNC,
    }
)


@dataclass(frozen=True, slots=True)
class ContractDescriptor:
    """One meaningful contract boundary -- a symbol with real evidence
    that something consumes it (spec section 4: "internal leaf helpers
    with no meaningful consumer surface should generally not generate
    Contract Intelligence"). Produced for every contract-eligible symbol,
    whether or not it actually changed this PR -- ``normalized_shape``
    is always the exact HEAD signature text, never a fabricated
    summary."""

    id: str
    kind: ContractKind
    qualified_name: str
    file_path: str
    externally_consumed: bool
    normalized_shape: str
    evidence: str


@dataclass(frozen=True, slots=True)
class ContractDelta:
    """One real, base-vs-head contract change -- never constructed
    unless both the base and head signatures were successfully,
    deterministically parsed (fail-closed: an unparseable signature
    produces no delta, never a guess)."""

    contract_id: str
    qualified_name: str
    file_path: str
    kind: ContractKind
    change_unit_id: str | None
    before_signature: str
    after_signature: str
    characteristics: tuple[BreakingCharacteristic, ...]
    evidence: str
    blast_radius: tuple[AffectedSymbolRef, ...] = field(default_factory=tuple)

    @property
    def is_potentially_breaking(self) -> bool:
        return any(c in BREAKING_CHARACTERISTICS for c in self.characteristics)


@dataclass(frozen=True, slots=True)
class ContractIntelligenceReport:
    """The complete, deterministic output for one review run. Never
    itself sent to an LLM in bulk -- only small, bounded per-candidate
    slices are (see
    :func:`patchfrog.contract_intelligence.evidence.evidence_text_for_candidate`)."""

    version: int
    descriptors: tuple[ContractDescriptor, ...]
    deltas: tuple[ContractDelta, ...]
    stale_consumers: tuple[ExpectedCompanionChange, ...]
    contract_story: str

    @property
    def potentially_breaking_deltas(self) -> tuple[ContractDelta, ...]:
        return tuple(d for d in self.deltas if d.is_potentially_breaking)

    @property
    def impacted_consumer_count(self) -> int:
        seen: set[tuple[str, str | None]] = set()
        for delta in self.deltas:
            for ref in delta.blast_radius:
                seen.add((ref.file_path, ref.qualified_name))
        return len(seen)

    @property
    def contract_kind_counts(self) -> dict[ContractKind, int]:
        counts: dict[ContractKind, int] = {}
        for descriptor in self.descriptors:
            counts[descriptor.kind] = counts.get(descriptor.kind, 0) + 1
        return counts
