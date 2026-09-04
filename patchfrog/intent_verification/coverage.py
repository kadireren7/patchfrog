"""Implementation-coverage and gap derivation (spec sections 9/11).

Never invents an affected surface -- every
:class:`~patchfrog.change_intelligence.domain.AffectedSymbolRef`/
:class:`~patchfrog.contract_intelligence.domain.ContractDelta`/
:class:`~patchfrog.change_intelligence.domain.ExpectedCompanionChange`
referenced here is an *existing* object J/K already computed. This
module only filters/tags that existing evidence by lexical relevance to
one :class:`~patchfrog.intent_verification.domain.IntentClaim` -- see
``validation/intent_verification/latest-summary.md`` section 2 for the
exact dedup design (why `PotentialIntentGap` is only ever constructed
for `EXPECTED_SURFACE_UNCHANGED`, never for a surface J/K already flag
via an `ExpectedCompanionChange`).
"""

from __future__ import annotations

from patchfrog.change_intelligence.domain import (
    ChangeUnit,
    CompanionStatus,
    ExpectedCompanionChange,
)
from patchfrog.contract_intelligence.domain import ContractDelta
from patchfrog.intent_verification.domain import (
    IntentClaim,
    IntentCoverage,
    IntentCoverageStatus,
    IntentGapReasonCode,
    PotentialIntentGap,
)
from patchfrog.intent_verification.lexical import meaningful_tokens


def derive_coverage_and_gaps(
    claim: IntentClaim,
    mapped: tuple[tuple[ChangeUnit, frozenset[str]], ...],
    *,
    contract_deltas: tuple[ContractDelta, ...],
    expected_companions: tuple[ExpectedCompanionChange, ...],
) -> tuple[IntentCoverage, tuple[PotentialIntentGap, ...]]:
    if not mapped:
        return (
            IntentCoverage(
                intent_claim_id=claim.id,
                status=IntentCoverageStatus.INSUFFICIENT_EVIDENCE,
                evidence="no ChangeUnit shares a meaningful term with this claim",
            ),
            (),
        )

    claim_terms = meaningful_tokens(claim.normalized_statement)
    mapped_unit_ids = tuple(unit.id for unit, _ in mapped)

    covered_surfaces: list[str] = []
    gaps: list[PotentialIntentGap] = []
    relevant_deltas: list[ContractDelta] = []
    relevant_companions: list[ExpectedCompanionChange] = []

    for unit, _shared_terms in mapped:
        for candidate in unit.changed_candidates:
            if candidate.qualified_name and candidate.qualified_name not in covered_surfaces:
                covered_surfaces.append(candidate.qualified_name)

        for ref in unit.affected_surface:
            ref_terms = meaningful_tokens(ref.qualified_name or "") | meaningful_tokens(ref.file_path)
            if not (ref_terms & claim_terms):
                continue
            gaps.append(
                PotentialIntentGap(
                    intent_claim_id=claim.id,
                    change_unit_id=unit.id,
                    expected_surface=ref,
                    reason_code=IntentGapReasonCode.EXPECTED_SURFACE_UNCHANGED,
                    evidence=(
                        f"{ref.qualified_name or ref.file_path!r} is {ref.relation.value} "
                        f"(distance {ref.distance}) on the same change this claim describes, "
                        f"but was not itself changed in this diff -- {ref.reason}"
                    ),
                )
            )

        relevant_deltas.extend(d for d in contract_deltas if d.change_unit_id == unit.id)
        relevant_companions.extend(c for c in expected_companions if c.change_unit_id == unit.id)

    has_missing_companion = any(c.status is CompanionStatus.MISSING for c in relevant_companions)
    if gaps or has_missing_companion:
        status = IntentCoverageStatus.PARTIAL_EVIDENCE
    else:
        status = IntentCoverageStatus.SUPPORTED

    coverage = IntentCoverage(
        intent_claim_id=claim.id,
        status=status,
        mapped_change_unit_ids=mapped_unit_ids,
        covered_surfaces=tuple(covered_surfaces),
        potentially_uncovered_surfaces=tuple(g.expected_surface for g in gaps),
        relevant_contract_deltas=tuple(relevant_deltas),
        relevant_companion_candidates=tuple(relevant_companions),
        evidence=f"mapped to {len(mapped_unit_ids)} ChangeUnit(s) via shared terms",
    )
    return coverage, tuple(gaps)
