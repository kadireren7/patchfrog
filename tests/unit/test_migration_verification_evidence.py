"""M8.9 / M8.10: coverage aggregation and evidence-strength assignment."""

from __future__ import annotations

from patchfrog.migration_verification.domain import (
    BaselineComparison,
    BaselineComparisonOutcome,
    CheckStatus,
    ContractCheckResult,
    EvidenceStrength,
    MigrationVerificationPlan,
    StepEvidence,
    VerificationRequirement,
    VerificationRequirementKind,
    VerificationStep,
    VerificationStepKind,
)
from patchfrog.migration_verification.evidence import build_coverage, compute_evidence_strength

_UNIT_TESTS_REQ = VerificationRequirement("r1", VerificationRequirementKind.UNIT_TESTS_PASS, "d", mandatory=True)


def _plan_with_step(*, blocks_verified: bool) -> MigrationVerificationPlan:
    step = VerificationStep(
        "st1", VerificationStepKind.TARGETED_UNIT_TEST, ("r1",), ("python", "-m", "pytest", "tests/test_x.py"),
        30.0, "reason", blocks_verified,
    )
    return MigrationVerificationPlan("c", "p", (_UNIT_TESTS_REQ,), (step,), ())


def test_fallback_non_blocking_failure_does_not_become_a_failed_requirement() -> None:
    plan = _plan_with_step(blocks_verified=False)
    evidence = (StepEvidence("st1", VerificationStepKind.TARGETED_UNIT_TEST, ("r1",), CheckStatus.FAILED, 1, 5.0,
                             "", "", "unrelated pre-existing failure"),)
    coverage = build_coverage(plan, step_evidence=evidence, contract_checks=())
    assert "r1" not in coverage.failed_requirement_ids
    assert "r1" in coverage.not_run_requirement_ids


def test_direct_blocking_failure_does_become_a_failed_requirement() -> None:
    plan = _plan_with_step(blocks_verified=True)
    evidence = (StepEvidence("st1", VerificationStepKind.TARGETED_UNIT_TEST, ("r1",), CheckStatus.FAILED, 1, 5.0,
                             "", "", "genuine failure"),)
    coverage = build_coverage(plan, step_evidence=evidence, contract_checks=())
    assert "r1" in coverage.failed_requirement_ids
    assert "r1" in coverage.failed_mandatory


def test_evidence_strength_strong_from_baseline_fail_patched_pass() -> None:
    comparisons = (BaselineComparison("r1", CheckStatus.FAILED, CheckStatus.PASSED,
                                      BaselineComparisonOutcome.BASELINE_FAIL_PATCHED_PASS, "d"),)
    strength = compute_evidence_strength(step_evidence=(), contract_checks=(), baseline_comparisons=comparisons)
    assert strength is EvidenceStrength.STRONG


def test_evidence_strength_strong_from_all_mandatory_contract_checks_passed() -> None:
    checks = (ContractCheckResult("r1", "d", CheckStatus.PASSED, "d"),)
    strength = compute_evidence_strength(step_evidence=(), contract_checks=checks, baseline_comparisons=())
    assert strength is EvidenceStrength.STRONG


def test_evidence_strength_none_when_nothing_gathered() -> None:
    strength = compute_evidence_strength(step_evidence=(), contract_checks=(), baseline_comparisons=())
    assert strength is EvidenceStrength.NONE


def test_many_weak_signals_never_promote_to_strong() -> None:
    evidence = tuple(
        StepEvidence(f"st{i}", VerificationStepKind.TYPE_CHECK, (f"r{i}",), CheckStatus.PASSED, 0, 1.0, "", "", "ok")
        for i in range(10)
    )
    strength = compute_evidence_strength(step_evidence=evidence, contract_checks=(), baseline_comparisons=())
    assert strength is EvidenceStrength.WEAK
