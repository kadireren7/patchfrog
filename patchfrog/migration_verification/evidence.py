"""Evidence strength assignment (M8.10) and evidence-bundle assembly (M8.9).

Never let many weak checks masquerade as one strong proof: strength is
assigned from the single best piece of evidence actually gathered, not
from how many weak signals accumulated.
"""

from __future__ import annotations

from datetime import UTC, datetime

from patchfrog.migration_verification.domain import (
    MIGRATION_VERIFICATION_VERSION,
    BaselineComparison,
    BaselineComparisonOutcome,
    CheckStatus,
    ContractCheckResult,
    EvidenceStrength,
    MigrationEvidenceBundle,
    MigrationVerificationPlan,
    ResidualRisk,
    StepEvidence,
    VerificationCoverage,
    VerificationOutcome,
    VerificationStepKind,
)


def compute_evidence_strength(
    *,
    step_evidence: tuple[StepEvidence, ...],
    contract_checks: tuple[ContractCheckResult, ...],
    baseline_comparisons: tuple[BaselineComparison, ...],
) -> EvidenceStrength:
    if any(c.outcome is BaselineComparisonOutcome.BASELINE_FAIL_PATCHED_PASS for c in baseline_comparisons):
        return EvidenceStrength.STRONG
    mandatory_contract_checks = list(contract_checks)
    if mandatory_contract_checks and all(c.status is CheckStatus.PASSED for c in mandatory_contract_checks):
        return EvidenceStrength.STRONG

    if any(c.outcome is BaselineComparisonOutcome.BASELINE_PASS_PATCHED_PASS for c in baseline_comparisons):
        return EvidenceStrength.MODERATE
    direct_or_transitive_tests_passed = any(
        e.kind is VerificationStepKind.TARGETED_UNIT_TEST and e.status is CheckStatus.PASSED
        for e in step_evidence
    )
    if direct_or_transitive_tests_passed:
        return EvidenceStrength.MODERATE

    if any(e.kind is VerificationStepKind.TYPE_CHECK and e.status is CheckStatus.PASSED for e in step_evidence):
        return EvidenceStrength.WEAK
    if any(e.status is CheckStatus.PASSED for e in step_evidence):
        return EvidenceStrength.WEAK
    return EvidenceStrength.NONE


def build_coverage(
    plan: MigrationVerificationPlan,
    *,
    step_evidence: tuple[StepEvidence, ...],
    contract_checks: tuple[ContractCheckResult, ...],
) -> VerificationCoverage:
    blocks_verified_by_step = {s.step_id: s.blocks_verified for s in plan.steps}
    status_by_requirement: dict[str, CheckStatus] = {}
    for evidence in step_evidence:
        status = evidence.status
        if status is CheckStatus.FAILED and not blocks_verified_by_step.get(evidence.step_id, True):
            # A non-blocking step (e.g. a FALLBACK-classified targeted
            # test -- M8.3's own weak-evidence tier, or the best-effort
            # type check) failing is never treated as proof a requirement
            # is unmet: that would let an unrelated, pre-existing failure
            # ("unrelated-test trap", M8.14) force a hard
            # FAILED/REGRESSION_DETECTED verdict. The raw evidence (with
            # its own detail/excerpt) is still fully preserved in
            # ``step_evidence`` -- only the aggregated per-requirement
            # coverage treats it as inconclusive rather than damning.
            status = CheckStatus.NOT_RUN
        for requirement_id in evidence.requirement_ids:
            status_by_requirement[requirement_id] = _combine(status_by_requirement.get(requirement_id), status)
    for check in contract_checks:
        status_by_requirement[check.requirement_id] = _combine(status_by_requirement.get(check.requirement_id), check.status)

    satisfied: list[str] = []
    failed: list[str] = []
    not_run: list[str] = []
    unavailable: list[str] = []
    for requirement in plan.requirements:
        status = status_by_requirement.get(requirement.requirement_id, CheckStatus.NOT_RUN)
        if status is CheckStatus.PASSED:
            satisfied.append(requirement.requirement_id)
        elif status is CheckStatus.FAILED:
            failed.append(requirement.requirement_id)
        elif status is CheckStatus.UNAVAILABLE:
            unavailable.append(requirement.requirement_id)
        else:
            not_run.append(requirement.requirement_id)

    return VerificationCoverage(
        mandatory_requirement_ids=tuple(sorted(plan.mandatory_requirement_ids)),
        satisfied_requirement_ids=tuple(sorted(satisfied)),
        failed_requirement_ids=tuple(sorted(failed)),
        not_run_requirement_ids=tuple(sorted(not_run)),
        unavailable_requirement_ids=tuple(sorted(unavailable)),
    )


def _combine(existing: CheckStatus | None, new: CheckStatus) -> CheckStatus:
    """A requirement backed by more than one piece of evidence (rare,
    e.g. a requirement both a step and a contract check reference) never
    silently drops a failure in favor of a later pass."""

    if existing is None:
        return new
    if CheckStatus.FAILED in (existing, new):
        return CheckStatus.FAILED
    if CheckStatus.PASSED in (existing, new):
        return CheckStatus.PASSED
    if CheckStatus.UNAVAILABLE in (existing, new):
        return CheckStatus.UNAVAILABLE
    return existing


def compute_residual_risk(
    coverage: VerificationCoverage, *, evidence_strength: EvidenceStrength, has_unresolved_human_steps: bool
) -> ResidualRisk:
    if coverage.failed_mandatory or has_unresolved_human_steps:
        return ResidualRisk.HIGH
    if coverage.unresolved_mandatory or evidence_strength in (EvidenceStrength.WEAK, EvidenceStrength.NONE):
        return ResidualRisk.MEDIUM
    return ResidualRisk.LOW


def build_evidence_bundle(
    *,
    plan: MigrationVerificationPlan,
    repository: str,
    repository_head_sha: str,
    step_evidence: tuple[StepEvidence, ...],
    contract_checks: tuple[ContractCheckResult, ...],
    baseline_comparisons: tuple[BaselineComparison, ...],
    coverage: VerificationCoverage,
    evidence_strength: EvidenceStrength,
    residual_risk: ResidualRisk,
    outcome: VerificationOutcome,
    outcome_reasons: tuple[str, ...],
) -> MigrationEvidenceBundle:
    return MigrationEvidenceBundle(
        version=MIGRATION_VERIFICATION_VERSION,
        change_fingerprint=plan.change_fingerprint,
        repository=repository,
        repository_head_sha=repository_head_sha,
        patch_fingerprint=plan.patch_fingerprint,
        plan_fingerprint=plan.fingerprint(),
        plan=plan,
        step_evidence=step_evidence,
        contract_checks=contract_checks,
        baseline_comparisons=baseline_comparisons,
        coverage=coverage,
        evidence_strength=evidence_strength,
        residual_risk=residual_risk,
        outcome=outcome,
        outcome_reasons=outcome_reasons,
        generated_at_iso=datetime.now(UTC).isoformat(),
        unresolved_requirement_ids=coverage.unresolved_mandatory,
    )


__all__ = [
    "build_coverage",
    "build_evidence_bundle",
    "compute_evidence_strength",
    "compute_residual_risk",
]
