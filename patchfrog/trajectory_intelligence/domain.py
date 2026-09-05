"""Pure domain model for Trajectory Intelligence -- no I/O, no LLM
(mirrors :mod:`patchfrog.historical_regression_memory.domain`/
:mod:`patchfrog.repository_learnings.domain`'s own role).

**Product principle (spec section 0): trajectory signals are never
findings.** This package answers "where should PatchFrog spend more
review attention?", never "where is the bug?". A trajectory signal may
only ever *strengthen orchestration* (deepen context, require critic,
reorder candidates) -- it is never published as a standalone warning,
never a churn/instability score, never a developer-performance
inference. See
``validation/trajectory_intelligence/latest-summary.md`` for the full
audit behind every scope decision below.

Reuses Phase 7's own already-persisted, already-ancestry-verified
:class:`~patchfrog.persistence.models.review_memory.ReviewGenerationModel`
lineage -- zero new git operations, zero new GitHub API calls, zero new
history crawler (audit section 1-5, 11).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID

#: Bumped whenever lineage-walking, event-derivation, signal-detection,
#: or hint-selection logic changes materially enough that a prior
#: report can no longer be considered equivalent to what re-running now
#: would produce. Independent of every prior Intelligence package's own
#: version (this package only *reads* Phase 5/Phase 7's already
#: -persisted rows; it never reinterprets their own rules).
TRAJECTORY_INTELLIGENCE_VERSION = 1

#: The lineage walk (see :mod:`patchfrog.trajectory_intelligence.queries`)
#: never examines more than this many `ReviewGenerationModel` rows,
#: whatever the PR's real history length -- "never scan unbounded
#: review history" (spec section 29).
MAX_TRAJECTORY_HEADS = 8

#: Bounds the total event list across every head/surface combined.
MAX_TRAJECTORY_EVENTS = 40

#: Bounds the final signal list per run.
MAX_TRAJECTORY_SIGNALS = 10

#: At most this many supporting events are kept per surface (mirrors
#: every other Intelligence package's own per-surface bounding
#: rationale) -- never an unbounded evidence list even if a surface was
#: touched at every single head.
MAX_EVENTS_PER_SURFACE = 6

#: Hard floor for `REPEATED_SURFACE_CHURN` -- the number of *distinct*
#: heads (by commit_sha) that must touch the exact same surface before
#: it counts as repeated churn. Deliberately higher than Milestone O's
#: own `MIN_SUPPORTING_EVENTS` (2): O's threshold counts independent
#: *trusted* events across separate reviews (already a strong signal by
#: construction); this counts mere repeated edits *within one PR's own
#: lineage*, which is common enough at 2 that a higher bar is needed to
#: keep the signal low-noise (audit section 7 -- "prefer precision and
#: low noise").
MIN_SURFACE_CHURN_EVENTS = 3


class TrajectoryEventKind(StrEnum):
    """Only `SURFACE_CHANGED`/`TEST_SURFACE_CHANGED` are ever
    constructed in v1 -- see
    ``validation/trajectory_intelligence/latest-summary.md`` sections
    6, 9, 10 for why `SURFACE_REMOVED`/`SURFACE_REINTRODUCED`/
    `CONTRACT_SURFACE_CHANGED` cannot be safely reconstructed from
    already-persisted data without either re-deriving cross-generation
    symbol continuity this codebase deliberately restricts to adjacent
    pairs, or fabricating per-head contract-change identity that is
    never persisted. All three are kept on the enum for forward
    documentation only, exactly mirroring Milestone N's own
    never-constructed ``HistoricalMatchKind.SAME_FILE``."""

    #: A `ReviewCandidateModel` with `reason == CHANGED_SYMBOL` at a
    #: given head, for a non-test file (`is_test_path` is False).
    SURFACE_CHANGED = "surface_changed"
    #: Same as above, for a test file (`is_test_path` is True).
    TEST_SURFACE_CHANGED = "test_surface_changed"
    #: Deferred -- see audit section 9.
    SURFACE_REMOVED = "surface_removed"
    #: Deferred -- see audit section 9.
    SURFACE_REINTRODUCED = "surface_reintroduced"
    #: Deferred -- see audit section 10.
    CONTRACT_SURFACE_CHANGED = "contract_surface_changed"


class TrajectorySignalKind(StrEnum):
    """Only `REPEATED_SURFACE_CHURN` is ever constructed in v1 -- see
    ``validation/trajectory_intelligence/latest-summary.md`` sections
    6-9 for the full audit of why `REVERT_LIKE_CYCLE`/
    `REINTRODUCED_SURFACE`/`PRODUCTION_THEN_TEST_FOLLOWUP` are deferred.
    All three are kept on the enum for forward documentation only."""

    #: The only signal kind implemented in v1: the exact same surface
    #: was touched (`SURFACE_CHANGED` or `TEST_SURFACE_CHANGED`) across
    #: at least :data:`MIN_SURFACE_CHURN_EVENTS` distinct heads in the
    #: current PR's own verified lineage.
    REPEATED_SURFACE_CHURN = "repeated_surface_churn"
    #: Deferred -- see audit section 6.
    REVERT_LIKE_CYCLE = "revert_like_cycle"
    #: Deferred -- see audit section 9.
    REINTRODUCED_SURFACE = "reintroduced_surface"
    #: Deferred -- see audit section 8.
    PRODUCTION_THEN_TEST_FOLLOWUP = "production_then_test_followup"


class TrajectoryReviewHint(StrEnum):
    """Deterministic from the strongest signal on a surface -- never an
    LLM decision (spec section 14). Only `REQUIRE_CRITIC` is ever
    selected by v1's single implemented signal kind;
    `DEEPEN_CONTEXT`/`INCREASE_CANDIDATE_PRIORITY` are reserved for
    future signal kinds and never selected today -- see
    ``validation/trajectory_intelligence/latest-summary.md`` section
    12. A candidate whose surface selects `REQUIRE_CRITIC` is *also*
    deterministically reordered to the front of the candidate list
    (spec section 36's `INCREASE_CANDIDATE_PRIORITY` behavior) as part
    of that same hint, not a second, independently-selected value."""

    NONE = "none"
    DEEPEN_CONTEXT = "deepen_context"
    REQUIRE_CRITIC = "require_critic"
    INCREASE_CANDIDATE_PRIORITY = "increase_candidate_priority"


@dataclass(frozen=True, slots=True)
class TrajectoryHead:
    """One head in the current PR's own verified lineage --
    `generation_id` is ``None`` only for the synthetic entry
    representing the *current*, in-progress review (its own
    `ReviewGenerationModel` row does not exist yet at the point this
    package runs -- see :mod:`patchfrog.trajectory_intelligence.service`'s
    own docstring)."""

    generation_id: UUID | None
    review_run_id: UUID | None
    commit_sha: str
    sequence_number: int
    observed_at: str


@dataclass(frozen=True, slots=True)
class TrajectoryEvent:
    """One structural fact: this exact surface was touched at this
    exact head. Never raw source, never a full diff -- only the bounded
    identity needed to detect a pattern."""

    head: TrajectoryHead
    file_path: str
    qualified_name: str
    event_kind: TrajectoryEventKind


@dataclass(frozen=True, slots=True)
class TrajectorySignal:
    """One deterministic pattern detected across a surface's own
    supporting events. Never a numeric risk probability -- only a
    signal kind, the events that produced it, and the one deterministic
    orchestration hint it selects."""

    surface_file_path: str
    surface_qualified_name: str
    signal_kind: TrajectorySignalKind
    supporting_events: tuple[TrajectoryEvent, ...]
    review_hint: TrajectoryReviewHint
    evidence: str

    @property
    def distinct_head_count(self) -> int:
        return len({e.head.commit_sha for e in self.supporting_events})


@dataclass(frozen=True, slots=True)
class TrajectoryIntelligenceReport:
    """The complete, deterministic output for one review run. Never
    itself sent to an LLM in bulk -- only small, bounded per-candidate
    slices are (see
    :func:`patchfrog.trajectory_intelligence.evidence.evidence_text_for_candidate`).
    Never rendered as a standalone user-facing section (spec section
    21) -- no ``story``/``summary`` field at all, unlike every other
    Intelligence package in this lineage."""

    version: int
    #: Whether *any* lineage could be established for this PR at all
    #: (a fresh PR with no prior generation is correctly ``False`` here
    #: -- never a hidden crash, never an ambiguous empty-vs-invalid
    #: state).
    lineage_valid: bool
    heads_considered: tuple[TrajectoryHead, ...]
    events: tuple[TrajectoryEvent, ...]
    signals: tuple[TrajectorySignal, ...]

    @property
    def head_count(self) -> int:
        return len(self.heads_considered)

    @property
    def event_count(self) -> int:
        return len(self.events)

    @property
    def signal_count(self) -> int:
        return len(self.signals)

    @property
    def repeated_surface_churn_count(self) -> int:
        return sum(1 for s in self.signals if s.signal_kind is TrajectorySignalKind.REPEATED_SURFACE_CHURN)

    @property
    def require_critic_count(self) -> int:
        return sum(1 for s in self.signals if s.review_hint is TrajectoryReviewHint.REQUIRE_CRITIC)

    @property
    def deepen_context_count(self) -> int:
        return sum(1 for s in self.signals if s.review_hint is TrajectoryReviewHint.DEEPEN_CONTEXT)
