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
    """**Never referenced by :class:`PotentialRepositoryLearningApplication`
    in v1** -- kept on the enum purely for forward documentation,
    exactly like ``HistoricalMatchKind.SAME_FILE`` in Milestone N.

    ``REPEATED_SAME_SURFACE_REGRESSION`` (the only implemented pattern
    kind) has no companion/consumer/test target to check for presence
    -- "this exact surface has repeatedly produced trusted findings" is
    historical-pattern *evidence*, not an expectation the current PR
    can satisfy or violate. An external-review correction round found
    the original v1 shape wrongly modeled this as ``UNSATISFIED``,
    implying the current PR fails some requirement -- it does not.
    ``SATISFIED``/``UNSATISFIED``/``INSUFFICIENT_EVIDENCE`` are
    reserved for a genuinely relational future pattern kind (anchor ->
    required companion) that encodes a real expectation to check. See
    ``validation/repository_learnings/latest-summary.md`` section 9b."""

    SATISFIED = "satisfied"
    UNSATISFIED = "unsatisfied"
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
    ``(repository_id, pattern_kind, anchor_file_path, anchor_qualified_name, finding_category)``;
    ``anchor_qualified_name`` is never ``None`` for the only
    implemented kind (a finding with no stable symbol identity cannot
    participate -- falling back to file-only identity would reintroduce
    exactly the over-broad match N's own correction round already
    ruled out for ``SAME_FILE``).

    **``finding_category`` is part of identity, not metadata** -- an
    external-review correction round found the original v1 shape took
    category from an arbitrary (earliest) supporting record while
    grouping purely on ``(file_path, qualified_name)``, which could
    silently combine two unrelated trusted findings on the same symbol
    (e.g. a SECURITY constant-time-comparison finding and an unrelated
    CORRECTNESS None-handling finding) into one fabricated "repeated
    pattern." Since no richer root-cause identity is persisted
    anywhere this package can safely read, category is the one
    additional structural signal available to avoid that -- two
    findings only support the same learning when they share it. This
    is conservative, not merely convenient: it can only ever *split* a
    would-be learning into two (or suppress it), never invent a false
    one."""

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
    """Bounded **enrichment** of an existing Milestone N
    :class:`~patchfrog.historical_regression_memory.domain.PotentialHistoricalRegression`
    with repeated, independently-trusted historical context -- never a
    standalone O warning, never a published finding on its own.

    **``enriches_historical_regression`` is mandatory, not optional.**
    An external-review correction round found the original v1 shape
    let this stand alone whenever the anchor was merely touched again,
    with no existing N candidate required -- that made O a second,
    independent historical-regression detector, exactly what it must
    never be (spec: "O must not simply wrap N under another label...
    O must never independently rediscover historical relevance").
    Fixed by requiring an existing N candidate on the *exact* same
    surface before any application is constructed at all: when no such
    N candidate exists this run, the learning simply produces no
    application (see :mod:`patchfrog.repository_learnings.matching`).

    Carries **no** ``status`` field -- ``REPEATED_SAME_SURFACE_REGRESSION``
    is historical-pattern evidence, not an invariant the current PR can
    satisfy or violate (see :class:`RepositoryLearningApplicationStatus`'s
    own docstring for why that enum is reserved, never referenced
    here)."""

    learning: RepositoryLearning
    current_change_unit_id: str
    current_file_path: str
    current_qualified_name: str | None
    evidence: str
    enriches_historical_regression: PotentialHistoricalRegression


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
