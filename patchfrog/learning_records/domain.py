"""Pure domain model for durable repository/organization learning
records (Milestone Y). No I/O, no LLM, no database session.

**This package never re-derives trust or repetition rules of its own.**
It is a thin, durable *snapshot* layer over evidence already computed by
Milestone O (:mod:`patchfrog.repository_learnings`, reused verbatim for
``USEFUL_FINDING_PATTERN``) and Milestone N's own trust-query shape
(mirrored, never duplicated in spirit, for ``NOISE_SUPPRESSION`` -- see
:mod:`patchfrog.learning_records.queries`). O's own domain deliberately
never persists a row of its own (see O's ``latest-summary.md`` sections
6/7); this package exists specifically to give Cloud (and any other
durable consumer) something to query without re-running a review.

**Product principle (repeated from the spec): repetition, never a
single event.** Every learning type here requires
:data:`MIN_SUPPORTING_EVENTS` *independent* (distinct ``finding_id`` AND
distinct ``review_run_id``) occurrences on the exact same structural
surface -- identical floor to Milestone O's own
``MIN_SUPPORTING_EVENTS``, independently declared here because this
package's own evidence sources (noise/useful surfaces) are distinct from
O's.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID

from patchfrog.analysis.domain import FindingCategory

#: Bumped whenever a learning type's derivation rule, maturity
#: threshold, or persisted-record shape changes materially enough that a
#: prior snapshot can no longer be considered equivalent to what
#: recomputing now would produce.
REPOSITORY_LEARNING_RECORD_VERSION = 1

#: Bumped whenever organization-level aggregation rules change
#: materially. Independent of REPOSITORY_LEARNING_RECORD_VERSION -- an
#: OrganizationLearning is derived *from* repository-level records, but
#: its own aggregation threshold/identity rules are a separate contract.
ORGANIZATION_LEARNING_VERSION = 1

#: Hard floor -- never configurable lower. Mirrors Milestone O's own
#: MIN_SUPPORTING_EVENTS exactly (two is the smallest number that can
#: ever mean "independently repeated," not "happened once").
MIN_SUPPORTING_EVENTS = 2

#: A learning is ESTABLISHED only once independent support strictly
#: exceeds the CANDIDATE floor -- "repeated positive feedback + multiple
#: independent review runs" per the spec's own Y3 example. Never a fake
#: percentage.
ESTABLISHED_SUPPORT_THRESHOLD = 3

#: Organization-level learning requires the pattern to be independently
#: ESTABLISHED in at least this many distinct repositories -- "require
#: stronger evidence than repo-level learning" (spec Y7).
ORG_MIN_ESTABLISHED_REPOSITORIES = 2

#: Bounds how many evidence references are kept per record (most-recent-
#: first) -- never an unbounded list even when a surface has many more
#: supporting events than this.
MAX_EVIDENCE_PER_RECORD = 5


class LearningType(StrEnum):
    """Only these three are ever constructed in v1 -- see the module
    docstring for why each maps onto already-existing evidence rather
    than a new detector."""

    #: Reuses Milestone O's own REPEATED_SAME_SURFACE_REGRESSION output
    #: verbatim (repeated fixed/useful trust on one exact surface).
    USEFUL_FINDING_PATTERN = "useful_finding_pattern"
    #: New: repeated, uncontradicted false-positive feedback on one
    #: exact surface (Y5). See queries.fetch_repeated_noise_surfaces.
    NOISE_SUPPRESSION = "noise_suppression"


class LearningMaturity(StrEnum):
    """Simple categorical maturity -- never a fake percentage (Y3)."""

    #: Minimum independent support met (>= MIN_SUPPORTING_EVENTS), not
    #: yet strong enough to be ESTABLISHED.
    CANDIDATE = "candidate"
    #: Independent support strictly exceeds ESTABLISHED_SUPPORT_THRESHOLD.
    ESTABLISHED = "established"
    #: Was CANDIDATE/ESTABLISHED; the most recent recomputation found the
    #: surface's supporting evidence contradicted (e.g. a NOISE_SUPPRESSION
    #: learning whose surface later received a useful/fixed signal).
    #: Never silently deleted -- see RepositoryLearningRecord's own
    #: docstring on history preservation.
    RETIRED = "retired"


class LearningScope(StrEnum):
    REPOSITORY = "repository"
    ORGANIZATION = "organization"


def classify_maturity(support_count: int) -> LearningMaturity:
    """Pure function, no I/O -- the only place CANDIDATE/ESTABLISHED
    thresholds are compared, so both are impossible to apply
    inconsistently across call sites."""

    if support_count >= ESTABLISHED_SUPPORT_THRESHOLD:
        return LearningMaturity.ESTABLISHED
    if support_count >= MIN_SUPPORTING_EVENTS:
        return LearningMaturity.CANDIDATE
    raise ValueError(
        f"support_count={support_count} is below MIN_SUPPORTING_EVENTS={MIN_SUPPORTING_EVENTS}; "
        "a learning must never be constructed at all below the repetition floor"
    )


@dataclass(frozen=True, slots=True)
class LearningEvidenceRef:
    """One independent occurrence backing a learning's support count --
    a reference (never a copy) of an existing ``ai_findings`` row plus
    the review run it came from."""

    finding_id: UUID
    review_run_id: UUID
    observed_at: str


@dataclass(frozen=True, slots=True)
class LearningSurface:
    """Structural identity a learning is about -- never semantic, never
    NLP/embedding-derived. Mirrors
    :class:`patchfrog.repository_learnings.domain.RepositoryLearningPattern`'s
    own identity fields exactly, for the same reason (category is part
    of identity, not metadata -- two findings only support the same
    learning when they share it)."""

    file_path: str
    qualified_name: str
    category: FindingCategory


@dataclass(frozen=True, slots=True)
class RepositoryLearningRecord:
    """A durable snapshot of one repository-scoped learning. ``id`` is
    ``None`` before the first persistence; once persisted, the same
    ``(repository_id, learning_type, surface)`` identity always upserts
    the same row -- recomputation is idempotent, never a growing history
    of duplicate rows for the same pattern."""

    id: UUID | None
    repository_id: UUID
    learning_type: LearningType
    surface: LearningSurface
    maturity: LearningMaturity
    support_count: int
    evidence: tuple[LearningEvidenceRef, ...]
    first_observed_at: str
    last_observed_at: str
    retired_reason: str | None = None
    version: int = REPOSITORY_LEARNING_RECORD_VERSION

    def explain(self) -> str:
        """Y9: a short, deterministic, human-readable explanation --
        never raw private code, only structural identity + counts
        already safe to show in a Cloud UI (see
        :mod:`patchfrog_cloud`'s own learning views)."""

        surface_desc = f"{self.surface.file_path}::{self.surface.qualified_name} ({self.surface.category.value})"
        if self.learning_type is LearningType.NOISE_SUPPRESSION:
            action = "repeatedly marked false-positive"
        else:
            action = "repeatedly confirmed useful/fixed"
        return (
            f"{surface_desc} was {action} across {self.support_count} independent review run(s), "
            f"first observed {self.first_observed_at}, last observed {self.last_observed_at} "
            f"-- maturity: {self.maturity.value}."
        )


@dataclass(frozen=True, slots=True)
class OrganizationLearning:
    """Y7: an aggregation of the *same* learning pattern independently
    ESTABLISHED in multiple repositories within one explicit,
    caller-provided scope (never a globally-inferred tenant -- see the
    module docstring and
    :mod:`patchfrog.learning_records.org_aggregation`)."""

    learning_type: LearningType
    surface_category: FindingCategory
    contributing_repository_ids: tuple[UUID, ...]
    contributing_records: tuple[RepositoryLearningRecord, ...]
    #: True only when an explicit, operator-registered
    #: ``RepositoryRelationModel`` edge exists between two contributing
    #: repositories (Y8) -- informational only, never proof of breakage.
    cross_repo_related: bool
    version: int = ORGANIZATION_LEARNING_VERSION

    @property
    def repository_count(self) -> int:
        return len(self.contributing_repository_ids)
