"""Pure domain model for Intent Verification -- no I/O, no LLM, no
database session (mirrors :mod:`patchfrog.change_intelligence.domain`/
:mod:`patchfrog.contract_intelligence.domain`'s own role).

Reuses :mod:`patchfrog.change_intelligence.domain` types directly where
the shape already fits (`AffectedSymbolRef` for the relevant-but-
unchanged surface, `ExpectedCompanionChange` referenced -- never
copied -- for dedup against J/K's own missing-companion/stale-consumer
evidence) -- see this package's own docstring and the audit in
``validation/intent_verification/latest-summary.md`` section 2 for why
nothing here duplicates those packages' candidate model.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from patchfrog.change_intelligence.domain import AffectedSymbolRef, ExpectedCompanionChange
from patchfrog.contract_intelligence.domain import ContractDelta

#: Bumped whenever extraction/sufficiency/mapping/coverage/gap logic
#: changes materially enough that a prior report can no longer be
#: considered equivalent to what re-running now would produce.
#: Independent of CHANGE_INTELLIGENCE_VERSION/CONTRACT_INTELLIGENCE_VERSION
#: (neither package's own logic changes because of this milestone -- see
#: docs/intent-verification.md).
INTENT_VERIFICATION_VERSION = 1

#: Spec section 7 -- never generate a requirements list from one PR
#: description.
MAX_INTENT_CLAIMS = 3

#: A claim maps to at most this many ChangeUnits -- bounded, deterministic,
#: never "the whole PR".
MAX_MAPPED_UNITS_PER_CLAIM = 2

#: Same defensive-bound rationale as
#: patchfrog.change_intelligence.service.MAX_CANDIDATES_CONSIDERED /
#: patchfrog.contract_intelligence.domain.MAX_CANDIDATES_CONSIDERED.
MAX_CANDIDATES_CONSIDERED = 150


class IntentSourceKind(StrEnum):
    """A small, intentionally non-exhaustive taxonomy (spec section 2).
    All five values are kept for forward extensibility/documentation,
    but **only `PR_TITLE`/`PR_BODY` are ever actually emitted as
    `IntentEvidence` this milestone**:

    - `PR_TITLE`/`PR_BODY` -- EXPLICIT, emitted by
      :func:`patchfrog.intent_verification.extraction.extract_claims_from_pr_metadata`.
    - `TEST_CHANGE` -- defined/reserved for a future milestone, **not
      emitted this milestone**. The real "changed tests strengthen
      coverage" signal this milestone actually provides is a different,
      simpler mechanism: an already-existing `TEST_NOT_UPDATED`
      :class:`~patchfrog.change_intelligence.domain.ExpectedCompanionChange`
      (Change Intelligence's own test-relationship evidence) that
      belongs to a claim's mapped `ChangeUnit` is referenced via
      `IntentCoverage.relevant_companion_candidates` -- never a
      `TEST_CHANGE`-kind `IntentEvidence` object, and never something
      that independently creates a claim. Implementing a real,
      standalone `TEST_CHANGE` evidence source (distinct bounded
      per-test signal, not just a companion reference) is deferred
      rather than half-built to satisfy the enum.
    - `LINKED_ISSUE`/`COMMIT_MESSAGE` -- deferred, not emitted -- see
      ``validation/intent_verification/latest-summary.md`` section 1
      for exactly why (no existing plumbing fetches either safely/
      cheaply today).
    """

    PR_TITLE = "pr_title"
    PR_BODY = "pr_body"
    LINKED_ISSUE = "linked_issue"
    COMMIT_MESSAGE = "commit_message"
    TEST_CHANGE = "test_change"


class IntentStrength(StrEnum):
    """`EXPLICIT` sources (PR title/body) can independently establish an
    :class:`IntentClaim`. `SUPPORTING` is reserved for a future source
    (see `IntentSourceKind.TEST_CHANGE`) that could strengthen/weaken an
    already-EXPLICIT claim's mapping without ever independently creating
    one (spec section 2's hard requirement) -- not actually assigned to
    any `IntentEvidence` this milestone, since none is emitted at
    `SUPPORTING` strength yet."""

    EXPLICIT = "explicit"
    SUPPORTING = "supporting"


@dataclass(frozen=True, slots=True)
class IntentEvidence:
    """One piece of raw evidence behind an :class:`IntentClaim`.
    ``bounded_text`` is already sanitized/truncated -- never the full
    raw PR body verbatim beyond the bound (see
    ``docs/intent-verification.md``'s Privacy section)."""

    source_kind: IntentSourceKind
    source_identifier: str
    bounded_text: str
    strength: IntentStrength


@dataclass(frozen=True, slots=True)
class IntentClaim:
    """One explicit, sufficiently-specific statement of intent -- never
    a paraphrase, never an LLM summary. ``normalized_statement`` is the
    sanitized/bounded source text itself (spec section 6: "a minimal
    deterministic claim may simply preserve a sanitized/bounded explicit
    statement" is an *acceptable*, not a fallback, design). ``id`` is a
    deterministic hash of ``(source_kind, normalized_statement)`` -- the
    same input always produces the same id, stable across a retry or a
    Phase 7 incremental re-review, never random (spec section 24)."""

    id: str
    normalized_statement: str
    source: IntentEvidence
    strength: IntentStrength


class IntentCoverageStatus(StrEnum):
    """Evidence-based states -- never a numeric percentage (spec
    section 9: "Do NOT create: 'Intent coverage = 74%'")."""

    #: Mapped to real change evidence, with no unresolved gap.
    SUPPORTED = "supported"
    #: Mapped, but at least one relevant surface remains unchanged or a
    #: relevant J/K companion candidate is still MISSING.
    PARTIAL_EVIDENCE = "partial_evidence"
    #: The claim could not be mapped to any real change evidence at all
    #: (spec section 8: "If mapping is ambiguous: leave the claim
    #: unmapped") -- never treated as a gap; simply not evaluable.
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"


class IntentGapReasonCode(StrEnum):
    """The full spec section 11 taxonomy is kept for documentation, but
    **only `EXPECTED_SURFACE_UNCHANGED` is ever used to construct a real
    `PotentialIntentGap`** this milestone -- see
    ``validation/intent_verification/latest-summary.md`` section 2 for
    why `CONTRACT_CONSUMER_STALE`/`EXPECTED_TEST_SURFACE_MISSING`
    describe cases already covered by an *existing* J/K
    `ExpectedCompanionChange` (surfaced via
    `IntentCoverage.relevant_companion_candidates` instead of a second,
    near-duplicate object), and `RELATED_PATH_UNCHANGED` is folded into
    `EXPECTED_SURFACE_UNCHANGED` (the distance/relation is carried in
    the gap's own evidence text, not a separate reason code)."""

    EXPECTED_SURFACE_UNCHANGED = "expected_surface_unchanged"
    RELATED_PATH_UNCHANGED = "related_path_unchanged"
    CONTRACT_CONSUMER_STALE = "contract_consumer_stale"
    EXPECTED_TEST_SURFACE_MISSING = "expected_test_surface_missing"


@dataclass(frozen=True, slots=True)
class PotentialIntentGap:
    """One candidate -- never a published finding on its own, exactly
    like :class:`~patchfrog.change_intelligence.domain.ExpectedCompanionChange`.
    ``expected_surface`` is always a real, already-computed
    :class:`~patchfrog.change_intelligence.domain.AffectedSymbolRef`
    (spec section 10: "Intent Verification must NOT invent affected
    surfaces from prose") that is lexically relevant to the claim but
    was not itself changed."""

    intent_claim_id: str
    change_unit_id: str
    expected_surface: AffectedSymbolRef
    reason_code: IntentGapReasonCode
    evidence: str


@dataclass(frozen=True, slots=True)
class IntentCoverage:
    """Evidence for one :class:`IntentClaim`, never a percentage. All
    referenced J/K objects (`relevant_contract_deltas`,
    `relevant_companion_candidates`) are the *exact same instances*
    already computed by those packages -- never copies, never
    re-derived."""

    intent_claim_id: str
    status: IntentCoverageStatus
    mapped_change_unit_ids: tuple[str, ...] = field(default_factory=tuple)
    covered_surfaces: tuple[str, ...] = field(default_factory=tuple)
    potentially_uncovered_surfaces: tuple[AffectedSymbolRef, ...] = field(default_factory=tuple)
    relevant_contract_deltas: tuple[ContractDelta, ...] = field(default_factory=tuple)
    relevant_companion_candidates: tuple[ExpectedCompanionChange, ...] = field(default_factory=tuple)
    evidence: str = ""


@dataclass(frozen=True, slots=True)
class IntentVerificationReport:
    """The complete, deterministic output for one review run. Never
    itself sent to an LLM in bulk -- only small, bounded per-candidate
    slices are (see
    :func:`patchfrog.intent_verification.evidence.evidence_text_for_candidate`)."""

    version: int
    claims: tuple[IntentClaim, ...]
    coverage: tuple[IntentCoverage, ...]
    gaps: tuple[PotentialIntentGap, ...]

    @property
    def mapped_claim_count(self) -> int:
        return sum(1 for c in self.coverage if c.mapped_change_unit_ids)

    @property
    def gap_count(self) -> int:
        return len(self.gaps)
