"""M8.7: deterministic contract verification, reusing M7's own
StepResult evidence rather than re-deriving it with string matching."""

from __future__ import annotations

from patchfrog.migration.domain import (
    AutoFixEligibility,
    EditOperation,
    GeneratedPatch,
    MigrationPlan,
    MigrationStatus,
    MigrationStep,
    MigrationStrategy,
    MigrationTarget,
    PatchOrigin,
    ResidualRisk,
    SafetyGateResult,
    StepOutcome,
    StepResult,
    VersionConstraint,
)
from patchfrog.migration_verification.contract import verify_contract
from patchfrog.migration_verification.domain import CheckStatus, VerificationRequirementKind
from patchfrog.migration_verification.requirements import generate_requirements

_TARGET = MigrationTarget("repo", "app/chat.py", "generate_reply", 3, "chat.create", "sdk_call", "python")


def _step(strategy: MigrationStrategy, *, version_constraint: VersionConstraint | None = None) -> MigrationStep:
    return MigrationStep("s1", _TARGET, strategy, AutoFixEligibility.AUTO_SAFE, ("d1",), ("k",), "o", "n", "n", (),
                         "", operation=EditOperation.of("op"), version_constraint=version_constraint)


def _plan(step: MigrationStep) -> MigrationPlan:
    return MigrationPlan("fp", "repo", "sha", (), (step,), ResidualRisk.LOW, MigrationStatus.PLANNED)


def _patch(*, outcome: StepOutcome, new_contents: dict[str, str] | None = None) -> GeneratedPatch:
    return GeneratedPatch(
        origin=PatchOrigin.DETERMINISTIC, unified_diff="diff", modified_files=("app/chat.py",),
        new_contents=new_contents or {"app/chat.py": "client.responses.create(...)"},
        step_results=(StepResult("s1", outcome, "detail", edits=1),),
        safety=(SafetyGateResult("gate", True, "ok"),),
    )


def test_applied_step_passes_its_contract_requirements() -> None:
    step = _step(MigrationStrategy.RENAME_SYMBOL)
    plan = _plan(step)
    requirements = generate_requirements(plan, test_selections=())
    checks = verify_contract(plan, _patch(outcome=StepOutcome.APPLIED), requirements)
    obsolete = next(c for c in checks if "obsolete" in c.description)
    assert obsolete.status is CheckStatus.PASSED
    aggregate = next(c for c in checks if c.requirement_id == next(
        r.requirement_id for r in requirements if r.kind is VerificationRequirementKind.CONTRACT_COMPATIBILITY_RESTORED
    ))
    assert aggregate.status is CheckStatus.PASSED


def test_failed_step_fails_its_contract_requirements_and_aggregate() -> None:
    step = _step(MigrationStrategy.RENAME_SYMBOL)
    plan = _plan(step)
    requirements = generate_requirements(plan, test_selections=())
    checks = verify_contract(plan, _patch(outcome=StepOutcome.FAILED), requirements)
    assert any(c.status is CheckStatus.FAILED for c in checks)
    aggregate = next(c for c in checks if c.requirement_id == next(
        r.requirement_id for r in requirements if r.kind is VerificationRequirementKind.CONTRACT_COMPATIBILITY_RESTORED
    ))
    assert aggregate.status is CheckStatus.FAILED


def test_version_constraint_mismatch_is_a_safety_net_failure() -> None:
    constraint = VersionConstraint("requirements.txt", "acme-ai", "pypi", "1.4.0", "2.0.0")
    step = _step(MigrationStrategy.BUMP_PACKAGE_VERSION, version_constraint=constraint)
    plan = _plan(step)
    requirements = generate_requirements(plan, test_selections=())
    # StepResult says APPLIED but the patched manifest doesn't actually
    # contain the target version -- the generator's own claim alone is
    # not proof; the content is re-checked.
    patch = _patch(outcome=StepOutcome.APPLIED, new_contents={"app/chat.py": "acme-ai==1.9.0"})
    checks = verify_contract(plan, patch, requirements)
    version_check = next(c for c in checks if "2.0.0" in c.description)
    assert version_check.status is CheckStatus.FAILED


def test_missing_step_result_is_unavailable_not_a_guess() -> None:
    step = _step(MigrationStrategy.RENAME_SYMBOL)
    plan = _plan(step)
    requirements = generate_requirements(plan, test_selections=())
    patch = GeneratedPatch(
        origin=PatchOrigin.DETERMINISTIC, unified_diff="", modified_files=(), new_contents={},
        step_results=(), safety=(),
    )
    checks = verify_contract(plan, patch, requirements)
    assert any(c.status is CheckStatus.UNAVAILABLE for c in checks)
