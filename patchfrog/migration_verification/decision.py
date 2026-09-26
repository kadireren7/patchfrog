"""Deterministic final verification decision engine (M8.11).

No LLM decides final truth here -- every branch is a pure function of
already-computed, explainable evidence. Mirrors the evidence-combination
discipline of :class:`patchfrog.fix_verification.domain.FixEvidenceDirection`:
a single strong contradicting signal wins outright; only strong, direct
evidence reaches ``VERIFIED``; weak evidence alone never promotes past
``PARTIALLY_VERIFIED``; missing evidence is ``UNVERIFIED``, never guessed.
"""

from __future__ import annotations

from patchfrog.migration_verification.domain import (
    EvidenceStrength,
    MigrationVerificationPlan,
    VerificationCoverage,
    VerificationOutcome,
)
from patchfrog.migration_verification.regression import RegressionFinding


def decide_outcome(
    plan: MigrationVerificationPlan,
    *,
    coverage: VerificationCoverage,
    evidence_strength: EvidenceStrength,
    regressions: tuple[RegressionFinding, ...],
    has_unresolved_human_steps: bool,
) -> tuple[VerificationOutcome, tuple[str, ...]]:
    # A regression is real, positive evidence of new damage -- it always
    # wins outright, regardless of how many other requirements passed.
    if regressions:
        return VerificationOutcome.REGRESSION_DETECTED, tuple(f"{r.reason_code}: {r.detail}" for r in regressions)

    if not plan.requirements:
        if has_unresolved_human_steps:
            return VerificationOutcome.HUMAN_REQUIRED, ("this migration has no automatic steps to verify; a human decision is required",)
        return VerificationOutcome.UNVERIFIED, ("nothing was generated for this migration to verify",)

    if coverage.failed_mandatory:
        return VerificationOutcome.FAILED, tuple(
            f"mandatory requirement failed: {requirement_id}" for requirement_id in coverage.failed_mandatory
        )

    unresolved = coverage.unresolved_mandatory
    if not unresolved:
        if has_unresolved_human_steps:
            # Every automatic requirement checks out, but the plan also
            # has steps this engine could not automate -- a human still
            # has to complete/confirm those before this migration is done.
            return VerificationOutcome.HUMAN_REQUIRED, (
                "all automatable requirements were verified, but the plan has unresolved human-required steps",
            )
        if evidence_strength in (EvidenceStrength.STRONG, EvidenceStrength.MODERATE):
            return VerificationOutcome.VERIFIED, (
                "all mandatory requirements satisfied", f"evidence strength: {evidence_strength.value}",
            )
        return VerificationOutcome.PARTIALLY_VERIFIED, (
            "mandatory requirements formally satisfied, but only weak or no supporting evidence was gathered",
        )

    if coverage.satisfied_requirement_ids:
        return VerificationOutcome.PARTIALLY_VERIFIED, tuple(
            f"unresolved mandatory requirement: {requirement_id}" for requirement_id in unresolved
        )

    return VerificationOutcome.UNVERIFIED, tuple(
        f"unresolved mandatory requirement: {requirement_id}" for requirement_id in unresolved
    ) or ("insufficient evidence to verify this migration",)


__all__ = ["decide_outcome"]
