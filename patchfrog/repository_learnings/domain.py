"""Pure domain model for Repository Learnings -- no I/O, no LLM, no
database session (mirrors :mod:`patchfrog.historical_regression_memory.domain`'s
own role, one layer further removed from the database: this package
never issues a query of its own at all -- see
:mod:`patchfrog.repository_learnings.matching`'s own docstring).

Reuses :mod:`patchfrog.historical_regression_memory.domain` types
directly (``HistoricalRegressionRecord`` for supporting evidence,
``PotentialHistoricalRegression`` referenced -- never copied -- for
dedup) -- see this package's own docstring and the audit in
``validation/repository_learnings/latest-summary.md`` for why nothing
here duplicates N's own trust model, temporal model, or candidate
model.

**Product principle (spec section 1): repetition, never a single
event.** Milestone N (Historical Regression Memory) can act on *one*
trusted historical finding. This package exists only to recognize a
*repeated, independent* pattern -- at least
:data:`MIN_SUPPORTING_EVENTS` genuinely separate, independently
trusted occurrences on the exact same structural surface. A single
trusted event, however strong, never produces a
:class:`RepositoryLearning` here.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID

from patchfrog.analysis.domain import FindingCategory
from patchfrog.historical_regression_memory.domain import (
    HistoricalRegressionRecord,
    PotentialHistoricalRegression,
)

#: Bumped whenever the minimum-support gate, independence rules,
#: pattern identity, activation-time computation, or application
#: matching changes materially enough that a prior report can no
#: longer be considered equivalent to what re-running now would
#: produce. Independent of HISTORICAL_REGRESSION_MEMORY_VERSION (this
#: package only *reads* N's already-fetched trusted records; it never
#: reinterprets N's own trust/temporal rules) and of every other
#: engine's own version.
REPOSITORY_LEARNINGS_VERSION = 1

#: Hard floor -- never configurable lower (spec: "repetition required,
#: never a single event"). Two is the smallest number that can ever
#: mean "independently repeated," not "happened once."
MIN_SUPPORTING_EVENTS = 2

#: At most this many supporting historical records are kept per
#: learning (strongest/most-recent-first, mirrors
#: patchfrog.historical_regression_memory.domain.MAX_HISTORICAL_RECORDS_PER_SURFACE's
#: own per-surface bounding rationale) -- never an unbounded evidence
#: list even if a surface has many more trusted findings than this.
MAX_SUPPORTING_EVENTS_PER_LEARNING = 5

#: Bounds the number of active learnings this package will ever derive
#: in one run -- mirrors every other engine's own per-run candidate cap.
MAX_LEARNINGS_PER_RUN = 10

#: Bounds the number of current-PR applications surfaced in one run --
#: mirrous patchfrog.historical_regression_memory.domain.MAX_HISTORICAL_REGRESSION_CANDIDATES's
#: own role.
MAX_APPLICATIONS_PER_RUN = 10


class RepositoryLearningPatternKind(StrEnum):
    """Only ``REPEATED_SAME_SURFACE_REGRESSION`` is ever constructed in
    v1 -- see ``validation/repository_learnings/latest-summary.md``
    section 3 for the full audit of why the other three kinds cannot
    be safely reconstructed from already-persisted data without either
    a new parallel history subsystem or inferring companion/consumer/
    test identity from finding prose (both out of scope). The other
    three members are kept on the enum for forward documentation only,
    exactly mirroring N's own never-constructed ``HistoricalMatchKind.SAME_FILE``."""

    #: The only pattern kind implemented in v1: at least
    #: ``MIN_SUPPORTING_EVENTS`` independent, trusted historical
    #: findings on the exact same ``(file_path, qualified_name)``
    #: surface.
    REPEATED_SAME_SURFACE_REGRESSION = "repeated_same_surface_regression"
    #: Deferred -- would require persisted per-pair (anchor, companion)
    #: identity per historical review run, which does not exist today.
    REPEATED_COMPANION_REQUIREMENT = "repeated_companion_requirement"
    #: Deferred -- would require persisted per-pair (anchor, consumer)
    #: identity per historical review run, which does not exist today.
    REPEATED_CONTRACT_CONSUMER_REQUIREMENT = "repeated_contract_consumer_requirement"
    #: Deferred -- would require persisted per-pair (anchor, test)
    #: identity per historical review run, which does not exist today.
    REPEATED_TEST_REQUIREMENT = "repeated_test_requirement"


class RepositoryLearningStatus(StrEnum):
    """Only ``ACTIVE`` is ever constructed -- see
    ``validation/repository_learnings/latest-summary.md`` section 6
    for why a below-threshold pattern is never represented as a
    ``CANDIDATE`` object at all (it simply is never constructed), and
    why an explicit ``RETIRED`` lifecycle is unnecessary (invalidation
    falls out naturally from re-deriving live every run -- see section
    7)."""

    ACTIVE = "active"


class RepositoryLearningApplicationStatus(StrEnum):
    """``SATISFIED``/``INSUFFICIENT_EVIDENCE`` are reserved for a
    future pattern kind with a real companion/consumer/test presence
    check (the only implemented kind,
    ``REPEATED_SAME_SURFACE_REGRESSION``, has no companion target to
    satisfy -- the anchor being touched again *is* the entire signal).
    See ``validation/repository_learnings/latest-summary.md`` section
    9. Never publish ``SATISFIED`` as praise/noise (spec section 22):
    an application object is only ever constructed when there is real,
    actionable context to add."""

    #: The only value ever constructed in v1: the learned surface is
    #: directly touched by the current PR again.
    UNSATISFIED = "unsatisfied"
    #: Reserved, never constructed in v1.
    SATISFIED = "satisfied"
    #: Reserved, never constructed in v1.
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"


@dataclass(frozen=True, slots=True)
class RepositoryLearningEvidence:
    """One trusted historical finding contributing to a learning's
    minimum support set -- references (never copies) N's own
    :class:`~patchfrog.historical_regression_memory.domain.HistoricalRegressionRecord`."""

    historical_record: HistoricalRegressionRecord


@dataclass(frozen=True, slots=True)
class RepositoryLearningPattern:
    """Structural identity of a learning -- never semantic, never NLP/
    embedding-derived. Identity is
    ``(repository_id, pattern_kind, anchor_file_path, anchor_qualified_name)``;
    ``anchor_qualified_name`` is never ``None`` for the only
    implemented kind (a finding with no stable symbol identity cannot
    participate -- falling back to file-only identity would reintroduce
    exactly the over-broad match N's own correction round already
    ruled out for ``SAME_FILE``)."""

    repository_id: UUID
    pattern_kind: RepositoryLearningPatternKind
    anchor_file_path: str
    anchor_qualified_name: str
    finding_category: FindingCategory


@dataclass(frozen=True, slots=True)
class RepositoryLearning:
    """A repeated, independently-trusted technical pattern this
    repository has demonstrated. Never persisted as its own row (see
    the audit's "Persistence decision") -- always re-derived live, per
    review run, from N's own already-fetched trusted records."""

    learning_id: str
    pattern: RepositoryLearningPattern
    status: RepositoryLearningStatus
    supporting_evidence: tuple[RepositoryLearningEvidence, ...]
    #: The number of *distinct historical review runs* backing this
    #: learning -- always >= MIN_SUPPORTING_EVENTS. Never simply
    #: ``len(supporting_evidence)`` conflated with raw record count:
    #: two findings from the same review run count once.
    support_count: int
    #: ISO-8601 timestamp: the moment the review-run-distinct support
    #: count first reached ``MIN_SUPPORTING_EVENTS`` -- i.e. the
    #: ``trusted_at`` of the last event in the *minimum* support set,
    #: never the most recent event overall.
    activated_at: str
    first_observed_at: str
    last_observed_at: str


@dataclass(frozen=True, slots=True)
class PotentialRepositoryLearningApplication:
    """One current-PR application of an active learning -- never a
    published finding on its own, exactly like every other J/K/L/M/N
    candidate type. ``enriches_historical_regression`` references an
    existing N candidate *by instance* (never copied) when the same
    surface is already flagged there -- see the audit's "Dedup
    ownership" section: for the only implemented pattern kind this is,
    in practice, always set, but the check is never hard-coded to
    assume so."""

    learning: RepositoryLearning
    current_change_unit_id: str
    current_file_path: str
    current_qualified_name: str | None
    status: RepositoryLearningApplicationStatus
    evidence: str
    enriches_historical_regression: PotentialHistoricalRegression | None = None

    @property
    def stands_alone(self) -> bool:
        return self.enriches_historical_regression is None


@dataclass(frozen=True, slots=True)
class RepositoryLearningsReport:
    """The complete, deterministic output for one review run. Never
    itself sent to an LLM in bulk -- only small, bounded per-candidate
    slices are (see
    :func:`patchfrog.repository_learnings.evidence.evidence_text_for_candidate`)."""

    version: int
    learnings_considered: tuple[RepositoryLearning, ...]
    applications: tuple[PotentialRepositoryLearningApplication, ...]
    repository_learning_story: str

    @property
    def learning_count(self) -> int:
        return len(self.learnings_considered)

    @property
    def application_count(self) -> int:
        return len(self.applications)
