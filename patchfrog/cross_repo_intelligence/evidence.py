"""Bounded, per-candidate evidence text for the reviewer prompt -- the
``<cross_repo_intelligence>`` section. Empty string (the common case)
for a candidate with no cross-repo overlap of its own.

**Never tells the LLM a conclusion**: neutral wording only ("use this
only as evidence that the current change may have external
compatibility impact; verify the current code independently"), never
"this will break repo B," never naming an author or ranking one
repository's change over another's."""

from __future__ import annotations

from patchfrog.cross_repo_intelligence.domain import CrossRepoIntelligenceReport, CrossRepoSignal
from patchfrog.review.domain import ReviewCandidate


def _text_for_signal(signal: CrossRepoSignal) -> str:
    return (
        f"contract: {', '.join(sorted({o.peer.contract_key for o in signal.supporting_overlaps}))}\n"
        f"pattern: {signal.signal_kind.value}\n"
        f"{signal.evidence}\n"
        "This repository is explicitly registered as producing a contract that another repository "
        "explicitly consumes. Use this only as evidence that the current change may have external "
        "compatibility impact -- verify the current code independently, never assume incompatibility."
    )


def evidence_text_for_candidate(report: CrossRepoIntelligenceReport, candidate: ReviewCandidate) -> str:
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
