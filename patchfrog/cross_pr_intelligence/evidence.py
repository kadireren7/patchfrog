"""Bounded, per-candidate evidence text for the reviewer prompt -- the
``<cross_pr_intelligence>`` section. Empty string (the common case)
for a candidate with no cross-PR overlap of its own -- keeps
LIGHT-tier candidates from ever seeing an inflated payload.

**Never tells the LLM a conclusion**: the wording is strictly neutral
("treat this only as a reason to check for conflicting behavior"),
never "this is probably a conflict," never naming a developer or
ranking one PR's change over another's."""

from __future__ import annotations

from patchfrog.cross_pr_intelligence.domain import CrossPRIntelligenceReport, CrossPRSignal
from patchfrog.review.domain import ReviewCandidate


def _text_for_signal(signal: CrossPRSignal) -> str:
    return (
        f"surface: {signal.surface_qualified_name}\n"
        f"pattern: {signal.signal_kind.value}\n"
        f"{signal.evidence}\n"
        "Treat this only as a reason to check for conflicting or duplicated behavior across these PRs -- "
        "it is not itself evidence of a defect in either one."
    )


def evidence_text_for_candidate(report: CrossPRIntelligenceReport, candidate: ReviewCandidate) -> str:
    if candidate.qualified_name is None:
        return ""
    relevant = [
        s
        for s in report.signals
        if s.surface_file_path == candidate.file_path and s.surface_qualified_name == candidate.qualified_name
    ]
    if not relevant:
        return ""
    return _text_for_signal(relevant[0])
