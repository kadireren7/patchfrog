"""Regression detection (M8.8) -- new damage the migration itself introduced.

Deliberately reuses evidence already gathered by the rest of the M8
pipeline (step evidence, contract checks, baseline comparisons, the
patch's own safety-gate results) rather than executing anything new.
``REGRESSION_DETECTED`` is always distinct from ``UNVERIFIED`` (M8.8's
own explicit requirement) -- a regression finding here means real,
positive evidence of new damage, never merely "we couldn't prove it's
fine".
"""

from __future__ import annotations

from dataclasses import dataclass

from patchfrog.migration.domain import GeneratedPatch, MigrationPlan
from patchfrog.migration_verification.domain import (
    BaselineComparison,
    BaselineComparisonOutcome,
    CheckStatus,
    ContractCheckResult,
    StepEvidence,
    VerificationStepKind,
)


@dataclass(frozen=True, slots=True)
class RegressionFinding:
    reason_code: str
    detail: str


def detect_regressions(
    plan: MigrationPlan,
    patch: GeneratedPatch,
    *,
    step_evidence: tuple[StepEvidence, ...],
    contract_checks: tuple[ContractCheckResult, ...],
    baseline_comparisons: tuple[BaselineComparison, ...],
) -> tuple[RegressionFinding, ...]:
    findings: list[RegressionFinding] = []

    for gate in patch.safety:
        if not gate.passed:
            findings.append(RegressionFinding("safety_gate_regression", f"{gate.gate}: {gate.detail}"))

    for evidence in step_evidence:
        if evidence.status is CheckStatus.FAILED and evidence.kind is VerificationStepKind.SYNTAX_CHECK:
            findings.append(RegressionFinding("syntax_failure", evidence.detail))
        # Deliberately NOT a regression signal: TYPE_CHECK failure alone.
        # PatchFrog's sandbox runs its own bundled mypy against the target
        # repository without that repository's own type-checking
        # configuration/stubs (see requirements.py's TYPE_CHECK_PASSES
        # docstring) -- a failure here does not independently prove the
        # migration introduced anything (CLAUDE.md's false-positive
        # discipline: a signal that cannot independently prove a defect
        # must never become one on its own). It remains WEAK evidence via
        # :mod:`patchfrog.migration_verification.evidence`, which caps
        # the achievable outcome at PARTIALLY_VERIFIED rather than
        # promoting it to a publication-blocking REGRESSION_DETECTED.

    for comparison in baseline_comparisons:
        if comparison.outcome is BaselineComparisonOutcome.BASELINE_PASS_PATCHED_FAIL:
            findings.append(RegressionFinding("targeted_test_regression", comparison.detail))

    for check in contract_checks:
        if check.status is CheckStatus.FAILED:
            findings.append(RegressionFinding("contract_mismatch", f"{check.description}: {check.detail}"))

    target_files = {s.target.file_path for s in plan.steps}
    unrelated = sorted(set(patch.modified_files) - target_files)
    for path in unrelated:
        findings.append(RegressionFinding("unrelated_file_modified", f"{path} was modified outside the migration's own step targets"))

    return tuple(findings)


__all__ = ["RegressionFinding", "detect_regressions"]
