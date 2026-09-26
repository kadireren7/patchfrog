"""Deterministic contract verification (M8.7).

Reuses M7's own already-computed
:class:`~patchfrog.migration.domain.StepResult`\\ s (the generator's own
record of whether each step's edit was applied, was already unnecessary,
or failed) rather than re-deriving the same evidence with string/regex
matching over the patched content. A step the generator itself reports
``APPLIED``/``NOT_NEEDED`` is real, parser-driven evidence that the
change was made; ``FAILED``/``SKIPPED``/``PENDING`` means it wasn't.

The one additional deterministic check this module adds beyond the
generator's own bookkeeping: for a version-constraint requirement, the
patched manifest content is checked to actually contain the target
version string -- a safety net against a `StepResult` that claims
`APPLIED` but produced content not matching what was promised.
"""

from __future__ import annotations

from patchfrog.migration.domain import GeneratedPatch, MigrationPlan, MigrationStep, StepOutcome
from patchfrog.migration_verification.domain import (
    CheckStatus,
    ContractCheckResult,
    VerificationRequirement,
    VerificationRequirementKind,
)

_CONTRACT_REQUIREMENT_KINDS = frozenset(
    {
        VerificationRequirementKind.OBSOLETE_SYMBOL_REMOVED,
        VerificationRequirementKind.SDK_CALL_MATCHES_CONTRACT,
        VerificationRequirementKind.REQUIRED_ARGUMENT_PRESENT,
        VerificationRequirementKind.VERSION_CONSTRAINT_COMPATIBLE,
    }
)

_STEP_OUTCOME_STATUS = {
    StepOutcome.APPLIED: CheckStatus.PASSED,
    StepOutcome.NOT_NEEDED: CheckStatus.PASSED,
    StepOutcome.FAILED: CheckStatus.FAILED,
    StepOutcome.SKIPPED: CheckStatus.NOT_RUN,
    StepOutcome.PENDING: CheckStatus.NOT_RUN,
}


def verify_contract(
    plan: MigrationPlan, patch: GeneratedPatch, requirements: tuple[VerificationRequirement, ...]
) -> tuple[ContractCheckResult, ...]:
    steps_by_id: dict[str, MigrationStep] = {s.step_id: s for s in plan.steps}
    results_by_step: dict[str, StepOutcome] = {r.step_id: r.outcome for r in patch.step_results}

    checks: list[ContractCheckResult] = []
    for requirement in requirements:
        if requirement.kind not in _CONTRACT_REQUIREMENT_KINDS or requirement.source_step_id is None:
            continue
        step = steps_by_id.get(requirement.source_step_id)
        outcome = results_by_step.get(requirement.source_step_id)
        if step is None or outcome is None:
            checks.append(
                ContractCheckResult(
                    requirement_id=requirement.requirement_id, description=requirement.description,
                    status=CheckStatus.UNAVAILABLE, detail="no generator step result found for this requirement",
                )
            )
            continue

        status = _STEP_OUTCOME_STATUS[outcome]
        detail = f"generator reported step {outcome.value!r} for {step.target.location}"

        if (
            requirement.kind is VerificationRequirementKind.VERSION_CONSTRAINT_COMPATIBLE
            and status is CheckStatus.PASSED
            and step.version_constraint is not None
        ):
            content = patch.new_contents.get(step.target.file_path)
            if content is None or step.version_constraint.target not in content:
                status = CheckStatus.FAILED
                detail = (
                    f"generator reported {outcome.value!r} but {step.target.file_path} does not contain "
                    f"target version {step.version_constraint.target!r}"
                )
            else:
                detail += f"; patched manifest contains target version {step.version_constraint.target!r}"

        checks.append(
            ContractCheckResult(
                requirement_id=requirement.requirement_id, description=requirement.description,
                status=status, detail=detail,
            )
        )

    aggregate = next(
        (r for r in requirements if r.kind is VerificationRequirementKind.CONTRACT_COMPATIBILITY_RESTORED), None
    )
    if aggregate is not None:
        per_step = list(checks)
        if per_step and all(c.status is CheckStatus.PASSED for c in per_step):
            agg_status, agg_detail = CheckStatus.PASSED, "every contract-specific check for this migration passed"
        elif any(c.status is CheckStatus.FAILED for c in per_step):
            agg_status, agg_detail = CheckStatus.FAILED, "at least one contract-specific check failed"
        else:
            agg_status, agg_detail = CheckStatus.NOT_RUN, "one or more contract-specific checks did not run"
        checks.append(
            ContractCheckResult(
                requirement_id=aggregate.requirement_id, description=aggregate.description,
                status=agg_status, detail=agg_detail,
            )
        )

    return tuple(checks)


__all__ = ["verify_contract"]
