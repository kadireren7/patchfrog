"""Conditional, deterministic Repository Learning summary -- a small,
bounded, evidence-only list, never a score/percentage/gamified badge
(spec section 20's own discipline, reused verbatim). Mirrors
:mod:`patchfrog.historical_regression_memory.summary`'s own discipline:
shown only when there is a real, current-PR application to report."""

from __future__ import annotations

from patchfrog.publishing.marker import sanitize_untrusted_text
from patchfrog.repository_learnings.domain import RepositoryLearningsReport

_MAX_LINES = 5


def should_render_repository_learning_summary(report: RepositoryLearningsReport) -> bool:
    return bool(report.applications)


def render_repository_learning_summary(report: RepositoryLearningsReport) -> str | None:
    if not report.applications:
        return None

    lines = ["### Repository learning", ""]
    for application in report.applications[:_MAX_LINES]:
        label = sanitize_untrusted_text(application.current_qualified_name or application.current_file_path)
        count = application.learning.support_count
        lines.append(
            f"- `{label}`: repeatedly produced trusted regressions across "
            f"{count} independent reviews."
        )

    return "\n".join(lines)
