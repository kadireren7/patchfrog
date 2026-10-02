"""Migration PR publication eligibility policy (M9.3).

A pure function of a verification outcome and operator policy -- never
silently lowers the bar. See
:class:`patchfrog.migration_pr.domain.MigrationPREligibility` for exactly
what each value means and
:class:`patchfrog.migration_verification.domain.VerificationOutcome` for
the outcome vocabulary this maps from.
"""

from __future__ import annotations

from patchfrog.migration_pr.domain import MigrationPREligibility, MigrationPRPolicy
from patchfrog.migration_verification.domain import VerificationOutcome


def determine_eligibility(
    outcome: VerificationOutcome, *, policy: MigrationPRPolicy
) -> tuple[MigrationPREligibility, str]:
    if outcome is VerificationOutcome.VERIFIED:
        return MigrationPREligibility.AUTO_OPEN, "migration verification outcome is VERIFIED"

    if outcome is VerificationOutcome.PARTIALLY_VERIFIED:
        if policy.allow_partially_verified:
            return (
                MigrationPREligibility.OPEN_WITH_OPERATOR_APPROVAL,
                "migration verification outcome is PARTIALLY_VERIFIED and operator policy allows publication "
                "(the PR will be visibly marked partial)",
            )
        return (
            MigrationPREligibility.NOT_ELIGIBLE,
            "migration verification outcome is PARTIALLY_VERIFIED and operator policy does not allow "
            "publishing a partially-verified migration (set allow_partially_verified=true to permit this)",
        )

    if outcome is VerificationOutcome.HUMAN_REQUIRED:
        return (
            MigrationPREligibility.PLAN_ONLY,
            "migration verification outcome is HUMAN_REQUIRED -- a plan/report may be produced, "
            "never an automatic code PR",
        )

    # UNVERIFIED, FAILED, REGRESSION_DETECTED
    return MigrationPREligibility.NOT_ELIGIBLE, f"migration verification outcome is {outcome.value.upper()}"


__all__ = ["determine_eligibility"]
