"""Top-level Repository Learnings orchestrator.

:func:`build_repository_learnings_report` is the one entry point
everything else in this package composes into. Called once per review
run, last in the Change -> Contract -> Intent -> Test -> Historical
Regression Memory -> Repository Learnings sequence, consuming N's own
already-fetched trusted records (``historical_regression_report.trusted_records_considered``)
and N's own already-computed candidates
(``historical_regression_report.candidates``) directly -- never a
second, duplicate SQL query for the same trust data, and never a
second, independent current-relevance check (see
:mod:`patchfrog.repository_learnings.matching`'s own docstring for why
this package takes no ``change_units`` parameter at all). See
:mod:`patchfrog.review.service`'s integration point.

Deliberately synchronous and session-free, exactly like
:mod:`patchfrog.test_intelligence.service`/
:mod:`patchfrog.intent_verification.service` -- see
``validation/repository_learnings/latest-summary.md`` section 16 for
why this milestone needs no repository-graph query or any new I/O at
all. Zero LLM calls.
"""

from __future__ import annotations

from uuid import UUID

from patchfrog.historical_regression_memory.domain import (
    HistoricalRegressionRecord,
    PotentialHistoricalRegression,
)
from patchfrog.repository_learnings.domain import (
    REPOSITORY_LEARNINGS_VERSION,
    RepositoryLearningsReport,
)
from patchfrog.repository_learnings.matching import (
    derive_repository_learning_applications,
    derive_repository_learnings,
)
from patchfrog.repository_learnings.story import build_repository_learning_story_prefix


def build_repository_learnings_report(
    *,
    repository_id: UUID,
    trusted_records: tuple[HistoricalRegressionRecord, ...] = (),
    historical_candidates: tuple[PotentialHistoricalRegression, ...] = (),
) -> RepositoryLearningsReport:
    learnings = derive_repository_learnings(trusted_records=trusted_records, repository_id=repository_id)
    if not learnings:
        return RepositoryLearningsReport(
            version=REPOSITORY_LEARNINGS_VERSION,
            learnings_considered=(),
            applications=(),
            repository_learning_story="",
        )

    applications = derive_repository_learning_applications(
        learnings=learnings, historical_candidates=historical_candidates
    )
    story = build_repository_learning_story_prefix(applications)

    return RepositoryLearningsReport(
        version=REPOSITORY_LEARNINGS_VERSION,
        learnings_considered=learnings,
        applications=applications,
        repository_learning_story=story,
    )
