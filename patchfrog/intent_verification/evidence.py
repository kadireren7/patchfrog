"""Bounded, per-candidate evidence text for the reviewer prompt (spec
section 15) -- the ``<intent_verification>`` section. Empty string (the
common case) for a candidate that isn't part of a claim's mapped
ChangeUnit; this is what keeps LIGHT-tier candidates from ever seeing an
inflated payload (Quality + Cost Guard remains authoritative, spec
section 15: "Do not massively increase prompt size.")."""

from __future__ import annotations

from patchfrog.change_intelligence.domain import CompanionStatus
from patchfrog.intent_verification.domain import IntentCoverage, IntentVerificationReport
from patchfrog.review.domain import ReviewCandidate

_MAX_LISTED = 5


def evidence_text_for_candidate(report: IntentVerificationReport, candidate: ReviewCandidate) -> str:
    if candidate.symbol_id is None:
        return ""

    relevant: list[tuple[str, IntentCoverage]] = []
    claims_by_id = {c.id: c for c in report.claims}
    for coverage in report.coverage:
        if not coverage.mapped_change_unit_ids:
            continue
        if candidate.qualified_name is None or candidate.qualified_name not in coverage.covered_surfaces:
            continue
        claim = claims_by_id.get(coverage.intent_claim_id)
        if claim is not None:
            relevant.append((claim.normalized_statement, coverage))

    if not relevant:
        return ""

    lines: list[str] = []
    for statement, coverage in relevant:
        lines.append(f"explicit_intent: {statement}")

        gaps = [g for g in report.gaps if g.intent_claim_id == coverage.intent_claim_id]
        if gaps:
            lines.append("potential_gap:")
            for gap in gaps[:_MAX_LISTED]:
                label = gap.expected_surface.qualified_name or gap.expected_surface.file_path
                lines.append(f"- {label}: {gap.evidence}")

        missing_companions = [
            c for c in coverage.relevant_companion_candidates if c.status is CompanionStatus.MISSING
        ]
        if missing_companions:
            lines.append("related_missing_surface:")
            for c in missing_companions[:_MAX_LISTED]:
                lines.append(f"- {c.expected_qualified_name}: {c.reason}")

        if coverage.relevant_contract_deltas:
            lines.append("related_contract_change:")
            for delta in coverage.relevant_contract_deltas[:_MAX_LISTED]:
                characteristics = ", ".join(c.value for c in delta.characteristics)
                lines.append(f"- {delta.qualified_name}: {characteristics}")

        if not gaps and not missing_companions:
            lines.append("note: no evidence-backed gap found -- explicit intent appears covered.")

    return "\n".join(lines)
