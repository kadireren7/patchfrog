"""Bounded, per-candidate evidence text for the reviewer prompt -- the
``<repository_learning>`` section. Empty string (the common case) for a
candidate with no active learning application of its own -- keeps
LIGHT-tier candidates from ever seeing an inflated payload.

Never includes the full historical review, old PR bodies, raw
historical source, or old finding discussion -- only the already-
bounded evidence sentence and support count."""

from __future__ import annotations

from patchfrog.repository_learnings.domain import RepositoryLearningsReport
from patchfrog.review.domain import ReviewCandidate

_MAX_LISTED = 3


def evidence_text_for_candidate(report: RepositoryLearningsReport, candidate: ReviewCandidate) -> str:
    relevant = [
        a
        for a in report.applications
        if a.current_file_path == candidate.file_path
        and (candidate.qualified_name is None or a.current_qualified_name == candidate.qualified_name)
    ]
    if not relevant:
        return ""

    lines: list[str] = []
    for a in relevant[:_MAX_LISTED]:
        lines.append(f"pattern: {a.learning.pattern.pattern_kind.value}")
        lines.append(f"support: {a.learning.support_count} independent trusted findings")
        lines.append(f"evidence: {a.evidence}")
    return "\n".join(lines)
