"""Bounded, per-candidate evidence text for the reviewer prompt -- the
``<historical_regression>`` section. Empty string (the common case) for
a candidate with no historical match of its own -- keeps LIGHT-tier
candidates from ever seeing an inflated payload (Quality + Cost Guard
remains authoritative, exactly like every other Intelligence package's
own evidence module).

Never includes the entire historical review, the full old PR body, raw
historical source, or a huge old finding discussion -- only the
already-bounded fingerprint and match reasoning (spec section 17)."""

from __future__ import annotations

from patchfrog.historical_regression_memory.domain import HistoricalRegressionReport
from patchfrog.review.domain import ReviewCandidate

_MAX_LISTED = 3


def evidence_text_for_candidate(report: HistoricalRegressionReport, candidate: ReviewCandidate) -> str:
    relevant = [
        c
        for c in report.candidates
        if c.current_file_path == candidate.file_path
        and (candidate.qualified_name is None or c.current_qualified_name == candidate.qualified_name)
    ]
    if not relevant:
        return ""

    lines: list[str] = []
    for c in relevant[:_MAX_LISTED]:
        lines.append(f"historical: {c.historical_record.bounded_evidence_fingerprint}")
        lines.append(f"outcome: {c.historical_record.evidence_strength.value}")
        lines.append(f"match: {c.match_kind.value}")
        lines.append(f"reason: {c.reason_code.value}")
    return "\n".join(lines)
