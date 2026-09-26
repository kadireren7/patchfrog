"""Machine-readable (JSON) and human-readable views of M8 evidence bundles.

The same dictionaries back ``--json`` CLI output and the bounded JSON
columns in :mod:`patchfrog.migration_verification.store`, mirroring
:mod:`patchfrog.migration.report`'s role for M7.
"""

from __future__ import annotations

from typing import Any

from patchfrog.migration_verification.domain import (
    BaselineComparison,
    ContractCheckResult,
    MigrationEvidenceBundle,
    MigrationVerificationPlan,
    StepEvidence,
    TestSelection,
    VerificationCoverage,
    VerificationRequirement,
    VerificationStep,
)


def requirement_to_dict(requirement: VerificationRequirement) -> dict[str, Any]:
    return {
        "requirement_id": requirement.requirement_id, "kind": requirement.kind.value,
        "description": requirement.description, "mandatory": requirement.mandatory,
        "diff_item_keys": list(requirement.diff_item_keys), "usage_site_key": requirement.usage_site_key,
        "source_step_id": requirement.source_step_id,
    }


def step_to_dict(step: VerificationStep) -> dict[str, Any]:
    return {
        "step_id": step.step_id, "kind": step.kind.value, "requirement_ids": list(step.requirement_ids),
        "command": list(step.command), "timeout_seconds": step.timeout_seconds, "reason": step.reason,
        "blocks_verified": step.blocks_verified,
        "test_classification": step.test_classification.value if step.test_classification else None,
    }


def test_selection_to_dict(selection: TestSelection) -> dict[str, Any]:
    return {"test_path": selection.test_path, "classification": selection.classification.value, "reason": selection.reason}


def plan_to_dict(plan: MigrationVerificationPlan) -> dict[str, Any]:
    return {
        "change_fingerprint": plan.change_fingerprint, "patch_fingerprint": plan.patch_fingerprint,
        "engine_version": plan.engine_version, "fingerprint": plan.fingerprint(),
        "requirements": [requirement_to_dict(r) for r in plan.requirements],
        "steps": [step_to_dict(s) for s in plan.steps],
        "test_selections": [test_selection_to_dict(s) for s in plan.test_selections],
    }


def step_evidence_to_dict(evidence: StepEvidence) -> dict[str, Any]:
    return {
        "step_id": evidence.step_id, "kind": evidence.kind.value, "requirement_ids": list(evidence.requirement_ids),
        "status": evidence.status.value, "exit_code": evidence.exit_code, "duration_ms": evidence.duration_ms,
        "stdout_excerpt": evidence.stdout_excerpt, "stderr_excerpt": evidence.stderr_excerpt, "detail": evidence.detail,
    }


def contract_check_to_dict(check: ContractCheckResult) -> dict[str, Any]:
    return {
        "requirement_id": check.requirement_id, "description": check.description, "status": check.status.value,
        "detail": check.detail,
    }


def baseline_comparison_to_dict(comparison: BaselineComparison) -> dict[str, Any]:
    return {
        "requirement_id": comparison.requirement_id, "baseline_status": comparison.baseline_status.value,
        "patched_status": comparison.patched_status.value, "outcome": comparison.outcome.value,
        "detail": comparison.detail,
    }


def coverage_to_dict(coverage: VerificationCoverage) -> dict[str, Any]:
    return {
        "mandatory_requirement_ids": list(coverage.mandatory_requirement_ids),
        "satisfied_requirement_ids": list(coverage.satisfied_requirement_ids),
        "failed_requirement_ids": list(coverage.failed_requirement_ids),
        "not_run_requirement_ids": list(coverage.not_run_requirement_ids),
        "unavailable_requirement_ids": list(coverage.unavailable_requirement_ids),
        "unresolved_mandatory": list(coverage.unresolved_mandatory),
    }


def bundle_to_dict(bundle: MigrationEvidenceBundle) -> dict[str, Any]:
    return {
        "version": bundle.version, "bundle_fingerprint": bundle.bundle_fingerprint,
        "change_fingerprint": bundle.change_fingerprint, "repository": bundle.repository,
        "repository_head_sha": bundle.repository_head_sha, "patch_fingerprint": bundle.patch_fingerprint,
        "plan_fingerprint": bundle.plan_fingerprint, "plan": plan_to_dict(bundle.plan),
        "step_evidence": [step_evidence_to_dict(e) for e in bundle.step_evidence],
        "contract_checks": [contract_check_to_dict(c) for c in bundle.contract_checks],
        "baseline_comparisons": [baseline_comparison_to_dict(b) for b in bundle.baseline_comparisons],
        "coverage": coverage_to_dict(bundle.coverage),
        "evidence_strength": bundle.evidence_strength.value, "residual_risk": bundle.residual_risk.value,
        "outcome": bundle.outcome.value, "outcome_reasons": list(bundle.outcome_reasons),
        "unresolved_requirement_ids": list(bundle.unresolved_requirement_ids),
        "generated_at": bundle.generated_at_iso,
    }


def render_bundle_text(bundle: MigrationEvidenceBundle) -> str:
    lines = [f"Verification outcome: {bundle.outcome.value.upper()}"]
    for reason in bundle.outcome_reasons:
        lines.append(f"  - {reason}")
    if bundle.contract_checks:
        lines.append("Contract:")
        for check in bundle.contract_checks:
            mark = "PASS" if check.status.value == "passed" else check.status.value.upper()
            lines.append(f"  {mark} -- {check.description}")
    test_steps = [e for e in bundle.step_evidence if e.kind.value in ("targeted_unit_test", "targeted_integration_test")]
    if test_steps:
        passed = sum(1 for e in test_steps if e.status.value == "passed")
        lines.append(f"Targeted tests: {passed}/{len(test_steps)} passed")
    type_checks = [e for e in bundle.step_evidence if e.kind.value == "type_check"]
    if type_checks:
        lines.append(f"Type checks: {type_checks[0].status.value.upper()}")
    if bundle.baseline_comparisons:
        lines.append("Baseline comparison:")
        for comparison in bundle.baseline_comparisons:
            lines.append(f"  {comparison.baseline_status.value} -> {comparison.patched_status.value} ({comparison.outcome.value})")
    lines.append(f"Residual risk: {bundle.residual_risk.value.upper()}")
    return "\n".join(lines) + "\n"


__all__ = [
    "baseline_comparison_to_dict",
    "bundle_to_dict",
    "contract_check_to_dict",
    "coverage_to_dict",
    "plan_to_dict",
    "render_bundle_text",
    "requirement_to_dict",
    "step_evidence_to_dict",
    "step_to_dict",
    "test_selection_to_dict",
]
