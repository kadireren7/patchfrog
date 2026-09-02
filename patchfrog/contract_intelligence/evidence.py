"""Bounded, per-candidate evidence text for the reviewer prompt (spec
section 12) -- the ``<contract_intelligence>`` section. Empty string
(the common case) for a candidate with no relevant contract delta;
this is what keeps LIGHT-tier candidates from ever seeing an inflated
payload (Quality + Cost Guard remains authoritative, spec section 12:
"Do not inflate every prompt.")."""

from __future__ import annotations

from patchfrog.change_intelligence.domain import CompanionStatus
from patchfrog.contract_intelligence.domain import ContractIntelligenceReport
from patchfrog.review.domain import ReviewCandidate

_MAX_LISTED = 5


def evidence_text_for_candidate(report: ContractIntelligenceReport, candidate: ReviewCandidate) -> str:
    if candidate.qualified_name is None:
        return ""

    delta = next((d for d in report.deltas if d.qualified_name == candidate.qualified_name), None)
    if delta is None:
        return ""

    consumers = [c for c in report.stale_consumers if c.source_qualified_name == candidate.qualified_name]
    observed = [c for c in consumers if c.status is CompanionStatus.OBSERVED]
    missing = [c for c in consumers if c.status is CompanionStatus.MISSING]

    lines = [
        f"contract: {delta.qualified_name}",
        f"before: {delta.before_signature.strip()}",
        f"after: {delta.after_signature.strip()}",
        f"characteristics: {', '.join(c.value for c in delta.characteristics)}",
    ]
    if observed:
        lines.append("updated_consumers:")
        lines.extend(f"- {c.expected_qualified_name}" for c in observed[:_MAX_LISTED])
    if missing:
        lines.append("potentially_stale_consumers:")
        lines.extend(f"- {c.expected_qualified_name}: {c.reason}" for c in missing[:_MAX_LISTED])
    if not delta.is_potentially_breaking:
        lines.append("note: characteristics above are backward-compatible; no stale-consumer concern.")

    return "\n".join(lines)
