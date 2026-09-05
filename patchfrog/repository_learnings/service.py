"""Top-level Repository Learnings orchestrator.

:func:`build_repository_learnings_report` is the one entry point
everything else in this package composes into. Called once per review
run, last in the Change -> Contract -> Intent -> Test -> Historical
Regression Memory -> Repository Learnings sequence, consuming N's own
already-fetched trusted records (``historical_regression_report.trusted_records_considered``)
directly -- never a second, duplicate SQL query for the same trust
data. See :mod:`patchfrog.review.service`'s integration point.

Deliberately synchronous and session-free, exactly like
:mod:`patchfrog.test_intelligence.service`/
:mod:`patchfrog.intent_verification.service` -- see
``validation/repository_learnings/latest-summary.md`` section 16 for
why this milestone needs no repository-graph query or any new I/O at
all. Zero LLM calls.
"""

from __future__ import annotations

from uuid import UUID

from patchfrog.change_intelligence.domain import ChangeUnit
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
    change_units: tuple[ChangeUnit, ...] = (),
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
        learnings=learnings, change_units=change_units, historical_candidates=historical_candidates
    )
    story = build_repository_learning_story_prefix(applications)

    return RepositoryLearningsReport(
        version=REPOSITORY_LEARNINGS_VERSION,
        learnings_considered=learnings,
        applications=applications,
        repository_learning_story=story,
    )
