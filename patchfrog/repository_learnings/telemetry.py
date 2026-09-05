"""Compact, persistence-ready summary of a
:class:`~patchfrog.repository_learnings.domain.RepositoryLearningsReport`
-- counts only. Unlike every prior Intelligence package, this one has
no rendered publication text to carry here: this package has no
separate conditional summary block in v1 (see this package's own
``__init__.py`` docstring for why) -- its only user-facing footprint is
the bounded Change Story addendum (folded directly into the existing
``review_runs.change_story`` text at the review-service integration
point, never a second column) and bounded per-candidate prompt
evidence, neither of which needs a persisted text column of its own."""

from __future__ import annotations

from dataclasses import dataclass

from patchfrog.repository_learnings.domain import RepositoryLearningsReport


@dataclass(frozen=True, slots=True)
class RepositoryLearningsSummary:
    version: int
    repository_learning_active_count: int
    repository_learning_application_count: int


def summarize_for_persistence(report: RepositoryLearningsReport) -> RepositoryLearningsSummary:
    return RepositoryLearningsSummary(
        version=report.version,
        repository_learning_active_count=report.learning_count,
        repository_learning_application_count=report.application_count,
    )
