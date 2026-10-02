"""M8.8: regression detection from already-gathered evidence."""

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
)
from patchfrog.migration_verification.domain import (
    BaselineComparison,
    BaselineComparisonOutcome,
    CheckStatus,
    StepEvidence,
    VerificationStepKind,
)
from patchfrog.migration_verification.regression import detect_regressions

_TARGET = MigrationTarget("repo", "app/chat.py", "generate_reply", 3, "chat.create", "sdk_call", "python")
_STEP = MigrationStep("s1", _TARGET, MigrationStrategy.RENAME_SYMBOL, AutoFixEligibility.AUTO_SAFE, (), (), "o", "n",
                      "n", (), "", operation=EditOperation.of("rename_symbol"))
_PLAN = MigrationPlan("fp", "repo", "sha", (), (_STEP,), ResidualRisk.LOW, MigrationStatus.PLANNED)


def _patch(*, safety_ok: bool = True, modified_files: tuple[str, ...] = ("app/chat.py",)) -> GeneratedPatch:
    return GeneratedPatch(
        origin=PatchOrigin.DETERMINISTIC, unified_diff="diff", modified_files=modified_files,
        new_contents=dict.fromkeys(modified_files, "content"),
        step_results=(StepResult("s1", StepOutcome.APPLIED, "applied", edits=1),),
        safety=(SafetyGateResult("gate", safety_ok, "detail"),),
    )


def test_safety_gate_failure_is_a_regression() -> None:
    findings = detect_regressions(_PLAN, _patch(safety_ok=False), step_evidence=(), contract_checks=(), baseline_comparisons=())
    assert any(f.reason_code == "safety_gate_regression" for f in findings)


def test_syntax_check_failure_is_a_regression() -> None:
    evidence = (StepEvidence("st1", VerificationStepKind.SYNTAX_CHECK, ("r1",), CheckStatus.FAILED, 1, 5.0, "", "err",
                             "py_compile failed"),)
    findings = detect_regressions(_PLAN, _patch(), step_evidence=evidence, contract_checks=(), baseline_comparisons=())
    assert any(f.reason_code == "syntax_failure" for f in findings)


def test_type_check_failure_alone_is_never_a_regression() -> None:
    # See regression.py's own docstring: PatchFrog's sandbox cannot prove
    # a type-check failure is newly introduced (no repo config/stubs) --
    # this must never independently trigger REGRESSION_DETECTED.
    evidence = (StepEvidence("st2", VerificationStepKind.TYPE_CHECK, ("r2",), CheckStatus.FAILED, 1, 5.0, "", "err",
                             "mypy reported type errors"),)
    findings = detect_regressions(_PLAN, _patch(), step_evidence=evidence, contract_checks=(), baseline_comparisons=())
    assert not any(f.reason_code == "type_failure" for f in findings)
    assert findings == ()


def test_baseline_pass_patched_fail_is_targeted_test_regression() -> None:
    comparisons = (BaselineComparison("r1", CheckStatus.PASSED, CheckStatus.FAILED,
                                      BaselineComparisonOutcome.BASELINE_PASS_PATCHED_FAIL, "detail"),)
    findings = detect_regressions(_PLAN, _patch(), step_evidence=(), contract_checks=(), baseline_comparisons=comparisons)
    assert any(f.reason_code == "targeted_test_regression" for f in findings)


def test_baseline_fail_patched_pass_is_never_a_regression() -> None:
    comparisons = (BaselineComparison("r1", CheckStatus.FAILED, CheckStatus.PASSED,
                                      BaselineComparisonOutcome.BASELINE_FAIL_PATCHED_PASS, "detail"),)
    findings = detect_regressions(_PLAN, _patch(), step_evidence=(), contract_checks=(), baseline_comparisons=comparisons)
    assert findings == ()


def test_unrelated_file_modification_is_flagged() -> None:
    patch = _patch(modified_files=("app/chat.py", "unrelated/other.py"))
    findings = detect_regressions(_PLAN, patch, step_evidence=(), contract_checks=(), baseline_comparisons=())
    assert any(f.reason_code == "unrelated_file_modified" and "unrelated/other.py" in f.detail for f in findings)


def test_clean_evidence_has_no_regressions() -> None:
    findings = detect_regressions(_PLAN, _patch(), step_evidence=(), contract_checks=(), baseline_comparisons=())
    assert findings == ()
