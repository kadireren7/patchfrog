"""Top-level Intent Verification orchestrator.

:func:`build_intent_verification_report` is the one entry point
everything else in this package composes into. Called once per review
run, after Change Intelligence and Contract Intelligence (whose already-
built evidence it consumes) -- see :mod:`patchfrog.review.service`'s
integration point.

Deliberately synchronous and session-free: unlike Change/Contract
Intelligence, this package never queries the repository graph itself --
every input is already-computed, in-memory evidence (PR title/body
strings, :class:`~patchfrog.change_intelligence.domain.ChangeUnit`\\ s,
:class:`~patchfrog.contract_intelligence.domain.ContractDelta`\\ s,
:class:`~patchfrog.change_intelligence.domain.ExpectedCompanionChange`\\ s).
Zero LLM calls, zero I/O.
"""

from __future__ import annotations

from patchfrog.change_intelligence.domain import ChangeUnit, ExpectedCompanionChange
from patchfrog.contract_intelligence.domain import ContractDelta
from patchfrog.intent_verification.coverage import derive_coverage_and_gaps
from patchfrog.intent_verification.domain import (
    INTENT_VERIFICATION_VERSION,
    IntentClaim,
    IntentCoverage,
    IntentVerificationReport,
    PotentialIntentGap,
)
from patchfrog.intent_verification.extraction import extract_claims_from_pr_metadata
from patchfrog.intent_verification.mapping import map_claim_to_units

_EMPTY_REPORT = IntentVerificationReport(
    version=INTENT_VERIFICATION_VERSION, claims=(), coverage=(), gaps=()
)


def build_intent_verification_report(
    *,
    title: str | None,
    body: str | None,
    change_units: tuple[ChangeUnit, ...] = (),
    contract_deltas: tuple[ContractDelta, ...] = (),
    expected_companions: tuple[ExpectedCompanionChange, ...] = (),
) -> IntentVerificationReport:
    claims = extract_claims_from_pr_metadata(title=title, body=body)
    if not claims:
        return _EMPTY_REPORT

    all_coverage: list[IntentCoverage] = []
    all_gaps: list[PotentialIntentGap] = []
    claim: IntentClaim
    for claim in claims:
        mapped = map_claim_to_units(claim, change_units)
        coverage, gaps = derive_coverage_and_gaps(
            claim, mapped, contract_deltas=contract_deltas, expected_companions=expected_companions
        )
        all_coverage.append(coverage)
        all_gaps.extend(gaps)

    return IntentVerificationReport(
        version=INTENT_VERIFICATION_VERSION,
        claims=claims,
        coverage=tuple(all_coverage),
        gaps=tuple(all_gaps),
    )
