"""M8.2: verification requirement generation from real migration steps."""

from __future__ import annotations

from patchfrog.migration.domain import (
    AutoFixEligibility,
    EditOperation,
    MigrationPlan,
    MigrationStatus,
    MigrationStep,
    MigrationStrategy,
    MigrationTarget,
    ResidualRisk,
    VersionConstraint,
)
from patchfrog.migration_verification.domain import VerificationRequirementKind
from patchfrog.migration_verification.requirements import generate_requirements


def _step(strategy: MigrationStrategy, *, version_constraint: VersionConstraint | None = None) -> MigrationStep:
    target = MigrationTarget("repo", "app/chat.py", "generate_reply", 3, "chat.create", "sdk_call", "python")
    return MigrationStep(
        "s1", target, strategy, AutoFixEligibility.AUTO_SAFE, ("d1",), ("sdk_symbol_renamed",), "old", "new", "new",
        (), "", operation=EditOperation.of("rename_symbol"), version_constraint=version_constraint,
    )


def _plan(*steps: MigrationStep) -> MigrationPlan:
    status = MigrationStatus.PLANNED if all(s.auto_fix for s in steps) else MigrationStatus.PARTIAL
    return MigrationPlan("fp", "repo", "deadbeef", (), steps, ResidualRisk.LOW, status)


def test_no_automatic_steps_yields_no_requirements() -> None:
    target = MigrationTarget("repo", "app/x.py", None, None, "x", "sdk_call", "python")
    step = MigrationStep("s1", target, MigrationStrategy.MANUAL_REVIEW, AutoFixEligibility.HUMAN_REQUIRED, (), (),
                         "", "", "", (), "")
    plan = MigrationPlan("fp", "repo", None, (), (step,), ResidualRisk.HIGH, MigrationStatus.HUMAN_REQUIRED)
    assert generate_requirements(plan, test_selections=()) == ()


def test_rename_symbol_generates_obsolete_symbol_and_contract_requirements() -> None:
    plan = _plan(_step(MigrationStrategy.RENAME_SYMBOL))
    requirements = generate_requirements(plan, test_selections=())
    kinds = {r.kind for r in requirements}
    assert VerificationRequirementKind.OBSOLETE_SYMBOL_REMOVED in kinds
    assert VerificationRequirementKind.SDK_CALL_MATCHES_CONTRACT in kinds
    assert VerificationRequirementKind.CONTRACT_COMPATIBILITY_RESTORED in kinds
    assert VerificationRequirementKind.SYNTAX_VALID in kinds
    assert VerificationRequirementKind.UNIT_TESTS_PASS in kinds
    obsolete = next(r for r in requirements if r.kind is VerificationRequirementKind.OBSOLETE_SYMBOL_REMOVED)
    assert obsolete.source_step_id == "s1"
    assert obsolete.mandatory


def test_syntax_is_mandatory_imports_and_typecheck_are_not() -> None:
    plan = _plan(_step(MigrationStrategy.RENAME_SYMBOL))
    requirements = generate_requirements(plan, test_selections=())
    by_kind = {r.kind: r for r in requirements}
    assert by_kind[VerificationRequirementKind.SYNTAX_VALID].mandatory
    assert not by_kind[VerificationRequirementKind.IMPORTS_RESOLVE].mandatory
    assert not by_kind[VerificationRequirementKind.TYPE_CHECK_PASSES].mandatory
    assert not by_kind[VerificationRequirementKind.CONSUMER_BEHAVIOR_PRESERVED].mandatory


def test_add_required_parameter_generates_required_argument_present() -> None:
    plan = _plan(_step(MigrationStrategy.ADD_REQUIRED_PARAMETER))
    requirements = generate_requirements(plan, test_selections=())
    assert any(r.kind is VerificationRequirementKind.REQUIRED_ARGUMENT_PRESENT for r in requirements)


def test_version_bump_generates_version_constraint_requirement() -> None:
    constraint = VersionConstraint("requirements.txt", "acme-ai", "pypi", "1.4.0", "2.0.0")
    plan = _plan(_step(MigrationStrategy.BUMP_PACKAGE_VERSION, version_constraint=constraint))
    requirements = generate_requirements(plan, test_selections=())
    version_req = next(r for r in requirements if r.kind is VerificationRequirementKind.VERSION_CONSTRAINT_COMPATIBLE)
    assert version_req.mandatory
    assert "2.0.0" in version_req.description
