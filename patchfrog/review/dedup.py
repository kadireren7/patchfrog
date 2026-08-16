"""AI/AI deduplication for accepted findings within one review run.

Static/AI duplicate suppression happens earlier and differently: the
critic is explicitly instructed (see :mod:`patchfrog.review.prompt`) to
``reject`` a proposal that merely restates a static finding without new
evidence, so a true static/AI duplicate is suppressed as a *rejected
proposal* (``REJECTED_CRITIC``), not as a dedup decision here -- rejecting
it earlier means it never reaches this stage, and the audit trail records
*why* (the critic's reasoning), which a bare "duplicate" dedup verdict
would not. This module only ever compares AI findings against each other,
after both already survived validation and the critic independently.

Two AI findings are considered duplicates when they target the same file
with overlapping line ranges and the same category -- a conservative rule
that only merges when it's very likely the same underlying bug. The
higher-severity (ties: higher-confidence) finding survives; deterministic
tie-break on ``(start_line, title)`` makes the outcome reproducible.
"""

from __future__ import annotations

from dataclasses import dataclass

from patchfrog.analysis.domain import Confidence, Severity
from patchfrog.review.domain import FinalAIFinding

_SEVERITY_RANK = {
    Severity.INFO: 0,
    Severity.LOW: 1,
    Severity.MEDIUM: 2,
    Severity.HIGH: 3,
    Severity.CRITICAL: 4,
}
_CONFIDENCE_RANK = {Confidence.LOW: 0, Confidence.MEDIUM: 1, Confidence.HIGH: 2}


@dataclass(frozen=True, slots=True)
class DedupResult:
    kept: tuple[FinalAIFinding, ...]
    suppressed: tuple[FinalAIFinding, ...]


def deduplicate(findings: tuple[FinalAIFinding, ...]) -> DedupResult:
    if len(findings) <= 1:
        return DedupResult(kept=findings, suppressed=())

    ordered = sorted(
        findings,
        key=lambda f: (
            f.finding.file_path,
            f.finding.start_line,
            -_SEVERITY_RANK[f.final_severity],
            -_CONFIDENCE_RANK[f.final_confidence],
            f.finding.title,
        ),
    )

    kept: list[FinalAIFinding] = []
    suppressed: list[FinalAIFinding] = []

    for candidate in ordered:
        duplicate_of = next((k for k in kept if _overlaps(k, candidate)), None)
        if duplicate_of is None:
            kept.append(candidate)
        else:
            suppressed.append(candidate)

    return DedupResult(kept=tuple(kept), suppressed=tuple(suppressed))


def _overlaps(a: FinalAIFinding, b: FinalAIFinding) -> bool:
    if a.finding.file_path != b.finding.file_path:
        return False
    if a.finding.category != b.finding.category:
        return False
    a_start, a_end = a.finding.start_line, a.finding.end_line
    b_start, b_end = b.finding.start_line, b.finding.end_line
    return not (a_end < b_start or b_end < a_start)
