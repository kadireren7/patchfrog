"""M8.11 / M8.14: the deterministic verification decision engine, covering
the milestone's own minimum fixture matrix at the decision-engine level
(pure, fast -- no sandbox needed). End-to-end sandboxed equivalents for
the baseline-evidence and human-required scenarios live in
``tests/integration/test_migration_verification_service.py`` against the
bundled demo repositories."""

from __future__ import annotations

from patchfrog.migration_verification.decision import decide_outcome
from patchfrog.migration_verification.domain import (
    EvidenceStrength,
    MigrationVerificationPlan,
    VerificationCoverage,
    VerificationOutcome,
    VerificationRequirement,
    VerificationRequirementKind,
)
from patchfrog.migration_verification.regression import RegressionFinding

_REQ = VerificationRequirement(
    requirement_id="r1", kind=VerificationRequirementKind.UNIT_TESTS_PASS, description="d", mandatory=True,
)
_REQ2 = VerificationRequirement(
    requirement_id="r2", kind=VerificationRequirementKind.CONTRACT_COMPATIBILITY_RESTORED, description="d",
    mandatory=True,
)
_PLAN = MigrationVerificationPlan(
    change_fingerprint="c", patch_fingerprint="p", requirements=(_REQ, _REQ2), steps=(), test_selections=(),
)
_EMPTY_PLAN = MigrationVerificationPlan(
    change_fingerprint="c", patch_fingerprint="p", requirements=(), steps=(), test_selections=(),
)


def _coverage(*, satisfied: tuple[str, ...] = (), failed: tuple[str, ...] = (), not_run: tuple[str, ...] = (),
             unavailable: tuple[str, ...] = ()) -> VerificationCoverage:
    return VerificationCoverage(
        mandatory_requirement_ids=("r1", "r2"), satisfied_requirement_ids=satisfied, failed_requirement_ids=failed,
        not_run_requirement_ids=not_run, unavailable_requirement_ids=unavailable,
    )


def test_baseline_fail_patched_pass_with_no_unresolved_reaches_verified() -> None:
    coverage = _coverage(satisfied=("r1", "r2"))
    outcome, reasons = decide_outcome(
        _PLAN, coverage=coverage, evidence_strength=EvidenceStrength.STRONG, regressions=(),
        has_unresolved_human_steps=False,
    )
    assert outcome is VerificationOutcome.VERIFIED
    assert reasons


def test_baseline_pass_patched_pass_is_moderate_and_still_verified() -> None:
    coverage = _coverage(satisfied=("r1", "r2"))
    outcome, _ = decide_outcome(
        _PLAN, coverage=coverage, evidence_strength=EvidenceStrength.MODERATE, regressions=(),
        has_unresolved_human_steps=False,
    )
    assert outcome is VerificationOutcome.VERIFIED


def test_patched_syntax_regression_is_regression_detected_even_with_other_evidence_satisfied() -> None:
    coverage = _coverage(satisfied=("r1", "r2"))
    outcome, reasons = decide_outcome(
        _PLAN, coverage=coverage, evidence_strength=EvidenceStrength.STRONG,
        regressions=(RegressionFinding("syntax_failure", "py_compile failed"),), has_unresolved_human_steps=False,
    )
    assert outcome is VerificationOutcome.REGRESSION_DETECTED
    assert "syntax_failure" in reasons[0]


def test_targeted_test_regression_baseline_pass_patched_fail() -> None:
    coverage = _coverage(satisfied=("r2",), failed=("r1",))
    outcome, _ = decide_outcome(
        _PLAN, coverage=coverage, evidence_strength=EvidenceStrength.WEAK,
        regressions=(RegressionFinding("targeted_test_regression", "baseline passed, patched failed"),),
        has_unresolved_human_steps=False,
    )
    assert outcome is VerificationOutcome.REGRESSION_DETECTED


def test_required_test_unavailable_is_unverified_or_partial_never_verified() -> None:
    # No other satisfied evidence at all -> insufficient evidence, UNVERIFIED.
    coverage = _coverage(unavailable=("r1", "r2"))
    outcome, _ = decide_outcome(
        _PLAN, coverage=coverage, evidence_strength=EvidenceStrength.NONE, regressions=(),
        has_unresolved_human_steps=False,
    )
    assert outcome is VerificationOutcome.UNVERIFIED


def test_contract_restored_without_runtime_test_is_partially_verified() -> None:
    # Contract check passed (r2), but the unit-test requirement (r1) has
    # no evidence at all -- strong contract evidence alone is never
    # enough to reach VERIFIED without any executable evidence.
    coverage = _coverage(satisfied=("r2",), unavailable=("r1",))
    outcome, reasons = decide_outcome(
        _PLAN, coverage=coverage, evidence_strength=EvidenceStrength.STRONG, regressions=(),
        has_unresolved_human_steps=False,
    )
    assert outcome is VerificationOutcome.PARTIALLY_VERIFIED
    assert any("r1" in r for r in reasons)


def test_migration_partially_fixed_mandatory_requirement_failed_is_failed_outcome() -> None:
    coverage = _coverage(satisfied=("r1",), failed=("r2",))
    outcome, reasons = decide_outcome(
        _PLAN, coverage=coverage, evidence_strength=EvidenceStrength.MODERATE, regressions=(),
        has_unresolved_human_steps=False,
    )
    assert outcome is VerificationOutcome.FAILED
    assert any("r2" in r for r in reasons)


def test_human_required_migration_with_no_automatic_requirements() -> None:
    outcome, reasons = decide_outcome(
        _EMPTY_PLAN, coverage=_coverage(), evidence_strength=EvidenceStrength.NONE, regressions=(),
        has_unresolved_human_steps=True,
    )
    assert outcome is VerificationOutcome.HUMAN_REQUIRED
    assert reasons


def test_human_required_when_automatic_part_verified_but_other_steps_remain() -> None:
    coverage = _coverage(satisfied=("r1", "r2"))
    outcome, _ = decide_outcome(
        _PLAN, coverage=coverage, evidence_strength=EvidenceStrength.STRONG, regressions=(),
        has_unresolved_human_steps=True,
    )
    assert outcome is VerificationOutcome.HUMAN_REQUIRED


def test_unrelated_test_trap_never_forces_failed_when_only_weak_evidence_is_unresolved() -> None:
    # A FALLBACK/non-blocking test's failure never enters `failed` in
    # coverage (see evidence.build_coverage) -- it shows up as an
    # unresolved (not_run) mandatory requirement instead, which is at
    # worst PARTIALLY_VERIFIED/UNVERIFIED, never a false FAILED.
    coverage = _coverage(satisfied=("r2",), not_run=("r1",))
    outcome, _ = decide_outcome(
        _PLAN, coverage=coverage, evidence_strength=EvidenceStrength.WEAK, regressions=(),
        has_unresolved_human_steps=False,
    )
    assert outcome in (VerificationOutcome.PARTIALLY_VERIFIED, VerificationOutcome.UNVERIFIED)


def test_dependency_conflict_is_contract_mismatch_regression() -> None:
    coverage = _coverage(satisfied=("r1",), failed=("r2",))
    outcome, reasons = decide_outcome(
        _PLAN, coverage=coverage, evidence_strength=EvidenceStrength.MODERATE,
        regressions=(RegressionFinding("contract_mismatch", "acme-ai constraint does not allow 2.0.0"),),
        has_unresolved_human_steps=False,
    )
    assert outcome is VerificationOutcome.REGRESSION_DETECTED
    assert "contract_mismatch" in reasons[0]


def test_no_requirements_and_no_human_steps_is_unverified_not_verified() -> None:
    # A stale/empty plan (e.g. re-verifying against a repository state
    # that no longer matches the patch) must never default to VERIFIED.
    outcome, reasons = decide_outcome(
        _EMPTY_PLAN, coverage=_coverage(), evidence_strength=EvidenceStrength.NONE, regressions=(),
        has_unresolved_human_steps=False,
    )
    assert outcome is VerificationOutcome.UNVERIFIED
    assert reasons


def test_human_required_dominates_partially_verified() -> None:
    coverage = _coverage(satisfied=("r2",), unavailable=("r1",))
    outcome, reasons = decide_outcome(
        _PLAN, coverage=coverage, evidence_strength=EvidenceStrength.STRONG, regressions=(),
        has_unresolved_human_steps=True,
    )
    assert outcome is VerificationOutcome.HUMAN_REQUIRED
    assert any("r1" in r for r in reasons)


def test_human_required_dominates_unverified() -> None:
    coverage = _coverage(unavailable=("r1", "r2"))
    outcome, _ = decide_outcome(
        _PLAN, coverage=coverage, evidence_strength=EvidenceStrength.NONE, regressions=(),
        has_unresolved_human_steps=True,
    )
    assert outcome is VerificationOutcome.HUMAN_REQUIRED


def test_human_required_dominates_weak_evidence_formal_satisfaction() -> None:
    outcome, _ = decide_outcome(
        _PLAN, coverage=_coverage(satisfied=("r1", "r2")), evidence_strength=EvidenceStrength.WEAK,
        regressions=(), has_unresolved_human_steps=True,
    )
    assert outcome is VerificationOutcome.HUMAN_REQUIRED


def test_failed_dominates_human_required() -> None:
    outcome, _ = decide_outcome(
        _PLAN, coverage=_coverage(satisfied=("r1",), failed=("r2",)), evidence_strength=EvidenceStrength.MODERATE,
        regressions=(), has_unresolved_human_steps=True,
    )
    assert outcome is VerificationOutcome.FAILED


def test_regression_dominates_failed_and_human_required() -> None:
    outcome, _ = decide_outcome(
        _PLAN, coverage=_coverage(failed=("r1",)), evidence_strength=EvidenceStrength.STRONG,
        regressions=(RegressionFinding("syntax_failure", "x"),), has_unresolved_human_steps=True,
    )
    assert outcome is VerificationOutcome.REGRESSION_DETECTED


def test_partially_verified_and_unverified_without_human_steps_unchanged() -> None:
    partial, _ = decide_outcome(
        _PLAN, coverage=_coverage(satisfied=("r2",), unavailable=("r1",)), evidence_strength=EvidenceStrength.STRONG,
        regressions=(), has_unresolved_human_steps=False,
    )
    unverified, _ = decide_outcome(
        _PLAN, coverage=_coverage(unavailable=("r1", "r2")), evidence_strength=EvidenceStrength.NONE,
        regressions=(), has_unresolved_human_steps=False,
    )
    assert partial is VerificationOutcome.PARTIALLY_VERIFIED
    assert unverified is VerificationOutcome.UNVERIFIED
