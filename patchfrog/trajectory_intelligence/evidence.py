"""Bounded, per-candidate evidence text for the reviewer prompt -- the
``<trajectory_intelligence>`` section. Empty string (the common case)
for a candidate with no trajectory signal of its own -- keeps
LIGHT-tier candidates from ever seeing an inflated payload.

**Never tells the LLM a conclusion** (spec section 20): the wording is
strictly neutral ("treat this only as a reason to inspect current
evidence carefully"), never "this is probably buggy," never a churn
count framed as a verdict."""

from __future__ import annotations

from patchfrog.review.domain import ReviewCandidate
from patchfrog.trajectory_intelligence.domain import TrajectoryIntelligenceReport, TrajectorySignal


def _text_for_signal(signal: TrajectorySignal) -> str:
    label = signal.surface_qualified_name
    return (
        f"surface: {label}\n"
        f"pattern: {signal.signal_kind.value}\n"
        f"distinct heads: {signal.distinct_head_count}\n"
        "This surface has undergone repeated structural change in the current PR lineage. "
        "Treat this only as a reason to inspect current evidence carefully -- it is not itself evidence of a defect."
    )


def evidence_text_for_candidate(report: TrajectoryIntelligenceReport, candidate: ReviewCandidate) -> str:
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
