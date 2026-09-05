"""Compact, persistence-ready summary of a
:class:`~patchfrog.repository_learnings.domain.RepositoryLearningsReport`
-- mirrors every other Intelligence package's own telemetry-split
exactly: this *persistence* summary carries the already-bounded,
already-rendered ``repository_learning_summary_text`` (needed for
cross-task publication), while the separate telemetry-snapshot type
(:class:`patchfrog.telemetry.domain.RepositoryLearningsTelemetry`)
stays counts-only. The Story prefix has no text field here at all --
it is folded directly into the existing ``review_runs.change_story``
text at the review-service integration point, never a second column."""

from __future__ import annotations

from dataclasses import dataclass

from patchfrog.repository_learnings.domain import RepositoryLearningsReport
from patchfrog.repository_learnings.summary import render_repository_learning_summary


@dataclass(frozen=True, slots=True)
class RepositoryLearningsSummary:
    version: int
    repository_learning_active_count: int
    repository_learning_application_count: int
    repository_learning_summary_rendered: bool
    repository_learning_summary_text: str | None


def summarize_for_persistence(report: RepositoryLearningsReport) -> RepositoryLearningsSummary:
    summary_text = render_repository_learning_summary(report)

    return RepositoryLearningsSummary(
        version=report.version,
        repository_learning_active_count=report.learning_count,
        repository_learning_application_count=report.application_count,
        repository_learning_summary_rendered=summary_text is not None,
        repository_learning_summary_text=summary_text,
    )
