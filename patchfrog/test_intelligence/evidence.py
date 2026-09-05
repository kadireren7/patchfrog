"""Bounded, per-candidate evidence text for the reviewer prompt -- the
``<test_intelligence>`` section. Empty string (the common case) for a
candidate with no gap of its own -- keeps LIGHT-tier candidates from
ever seeing an inflated payload (Quality + Cost Guard remains
authoritative, exactly like every other Intelligence package's own
evidence module)."""

from __future__ import annotations

from patchfrog.review.domain import ReviewCandidate
from patchfrog.test_intelligence.domain import TestIntelligenceReport

_MAX_LISTED = 5


def evidence_text_for_candidate(report: TestIntelligenceReport, candidate: ReviewCandidate) -> str:
    relevant = [
        gap
        for gap in report.gaps
        if gap.expectation.source_file_path == candidate.file_path
        and (candidate.qualified_name is None or gap.expectation.source_qualified_name == candidate.qualified_name)
    ]
    if not relevant:
        return ""

    lines: list[str] = []
    for gap in relevant[:_MAX_LISTED]:
        lines.append(f"{gap.expectation.reason_code.value}: {gap.expectation.evidence.bounded_text}")
    return "\n".join(lines)
