"""Pure domain model for Historical Regression Memory -- no I/O, no LLM,
no database session (mirrors :mod:`patchfrog.change_intelligence.domain`/
:mod:`patchfrog.contract_intelligence.domain`/
:mod:`patchfrog.intent_verification.domain`/
:mod:`patchfrog.test_intelligence.domain`'s own role).

Reuses :mod:`patchfrog.change_intelligence.domain` types directly where
the shape already fits (`AffectedSymbolRef` for the current surface
pool, `ExpectedCompanionChange` referenced -- never copied -- for dedup
against J/K's own candidates) -- see this package's own docstring and
the audit in ``validation/historical_regression_memory/latest-summary.md``
for why nothing here duplicates J/K/L/M's own candidate model or Phase
9's own feedback-trust model.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID

from patchfrog.analysis.domain import FindingCategory
from patchfrog.change_intelligence.domain import ExpectedCompanionChange
from patchfrog.intent_verification.domain import PotentialIntentGap
from patchfrog.test_intelligence.domain import PotentialTestGap

#: Bumped whenever trust-eligibility/matching/dedup logic changes
#: materially enough that a prior report can no longer be considered
#: equivalent to what re-running now would produce. Independent of
#: CHANGE_INTELLIGENCE_VERSION/CONTRACT_INTELLIGENCE_VERSION/
#: INTENT_VERIFICATION_VERSION/TEST_INTELLIGENCE_VERSION (none of those
#: packages' own logic changes because of this milestone) and of
#: FEEDBACK_ASSESSMENT_VERSION (this package only *reads* that version's
#: current output; it never reinterprets Phase 9's own rules).
HISTORICAL_REGRESSION_MEMORY_VERSION = 1

#: The one bounded SQL query this milestone issues per review run never
#: returns more than this many trusted historical records -- no N+1, no
#: per-surface query loop. See
#: :func:`patchfrog.historical_regression_memory.queries.fetch_trusted_historical_records`.
MAX_HISTORICAL_LOOKBACK_ROWS = 200

#: At most this many historical records are considered per matched
#: current surface (strongest trust/match/most-recent first, tie-broken
#: by id -- see :mod:`patchfrog.historical_regression_memory.matching`).
MAX_HISTORICAL_RECORDS_PER_SURFACE = 3

#: Bounds the final candidate list per run -- mirrors
#: patchfrog.test_intelligence.domain.MAX_TEST_GAPS_PER_UNIT's own role.
MAX_HISTORICAL_REGRESSION_CANDIDATES = 10

#: Never persist/forward more than this many characters of a historical
#: finding's own title as its "bounded evidence fingerprint" -- same
#: discipline as every other engine's own bounded-text fields.
MAX_EVIDENCE_FINGERPRINT_CHARS = 200


class HistoricalEvidenceStrength(StrEnum):
    """Only two states are backed by real, unambiguous persisted facts
    -- see ``validation/historical_regression_memory/latest-summary.md``
    section 2 for why ``REVIEW_ACCEPTED``/``WEAK`` (from the milestone's
    own illustrative sketch) are never implemented: neither corresponds
    to a concrete, already-persisted signal in this codebase, and
    inventing one would violate the "fail closed on ambiguity"
    requirement."""

    #: A developer explicitly replied ``/patchfrog fixed`` -- Phase 9's
    #: own strongest, least ambiguous signal (moves
    #: ``FeedbackAssessment.correctness_signal`` to POSITIVE directly).
    CONFIRMED_FIXED = "confirmed_fixed"
    #: A developer explicitly replied ``/patchfrog useful`` -- the first
    #: (strongest) branch of
    #: :func:`patchfrog.feedback.assessment.is_high_value_candidate`.
    CONFIRMED_USEFUL = "confirmed_useful"


class HistoricalMatchKind(StrEnum):
    """Implemented exactly as the spec's own hierarchy, strongest first
    -- no embeddings, no fuzzy matching, no NLP over old finding prose
    anywhere. See ``validation/historical_regression_memory/latest-summary.md``
    section 5 for the precise, non-overlapping definition of each."""

    SAME_SYMBOL = "same_symbol"
    SAME_QUALIFIED_NAME_IN_SAME_FILE = "same_qualified_name_in_same_file"
    SAME_FILE = "same_file"
    GRAPH_RELATED_SURFACE = "graph_related_surface"


class HistoricalRegressionReasonCode(StrEnum):
    """The spec's own 4-item taxonomy, no more -- see
    ``validation/historical_regression_memory/latest-summary.md``
    section 6 for the exact (match kind, trust) -> reason mapping."""

    PREVIOUS_FIXED_FINDING_SAME_SYMBOL = "previous_fixed_finding_same_symbol"
    PREVIOUS_USEFUL_FINDING_SAME_SYMBOL = "previous_useful_finding_same_symbol"
    PREVIOUS_FIXED_FINDING_SAME_FILE = "previous_fixed_finding_same_file"
    PREVIOUS_REGRESSION_RELATED_SURFACE = "previous_regression_related_surface"


@dataclass(frozen=True, slots=True)
class HistoricalRegressionRecord:
    """One trusted historical finding, fetched from already-persisted
    Phase 9/review data -- never a new parallel history store. Identity
    across historical and current review runs is always
    ``(source_file_path, source_qualified_name)``, never a
    ``symbol_id`` UUID (unstable across re-indexing -- see the audit).
    ``bounded_evidence_fingerprint`` is the finding's own bounded title,
    never its message/reasoning/suggested-fix/evidence-quote text."""

    historical_finding_id: UUID
    repository_id: UUID
    historical_review_run_id: UUID
    historical_commit_sha: str
    source_file_path: str
    source_qualified_name: str | None
    finding_category: FindingCategory
    evidence_strength: HistoricalEvidenceStrength
    bounded_evidence_fingerprint: str
    observed_at: str


@dataclass(frozen=True, slots=True)
class PotentialHistoricalRegression:
    """One candidate -- never a published finding on its own, exactly
    like every other J/K/L/M candidate type. ``enriches_companion``/
    ``enriches_intent_gap``/``enriches_test_gap`` reference an existing
    J/K/L/M object *by instance* (never copied) when the matched current
    surface is already owned by one of them -- see the audit's "Dedup
    ownership" section; at most one is ever set, and only when the
    matched surface is genuinely already flagged elsewhere."""

    current_change_unit_id: str
    current_file_path: str
    current_qualified_name: str | None
    historical_record: HistoricalRegressionRecord
    match_kind: HistoricalMatchKind
    reason_code: HistoricalRegressionReasonCode
    evidence: str
    enriches_companion: ExpectedCompanionChange | None = None
    enriches_intent_gap: PotentialIntentGap | None = None
    enriches_test_gap: PotentialTestGap | None = None

    @property
    def stands_alone(self) -> bool:
        return (
            self.enriches_companion is None
            and self.enriches_intent_gap is None
            and self.enriches_test_gap is None
        )


@dataclass(frozen=True, slots=True)
class HistoricalRegressionReport:
    """The complete, deterministic output for one review run. Never
    itself sent to an LLM in bulk -- only small, bounded per-candidate
    slices are (see
    :func:`patchfrog.historical_regression_memory.evidence.evidence_text_for_candidate`)."""

    version: int
    trusted_records_considered: tuple[HistoricalRegressionRecord, ...]
    candidates: tuple[PotentialHistoricalRegression, ...]
    historical_story: str

    @property
    def candidate_count(self) -> int:
        return len(self.candidates)

    @property
    def match_kind_counts(self) -> dict[HistoricalMatchKind, int]:
        counts: dict[HistoricalMatchKind, int] = {}
        for c in self.candidates:
            counts[c.match_kind] = counts.get(c.match_kind, 0) + 1
        return counts
