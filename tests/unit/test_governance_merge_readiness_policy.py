"""Z5 -- Merge Readiness + policy integration corpus. Never constructs a
second Merge Readiness engine; every test wraps a real, hand-built
:class:`MergeReadinessResult` exactly as
:meth:`MergeReadinessService.evaluate` would have produced it (spec
tests Z13-Z17)."""

from __future__ import annotations

import uuid

from patchfrog.analysis.domain import FindingCategory, Severity
from patchfrog.governance.domain import PolicyReasonCode, PolicyRule, PolicyScope
from patchfrog.governance.merge_readiness_policy import (
    PolicyRelevantFinding,
    VerificationSignal,
    apply_policy_to_merge_readiness,
)
from patchfrog.governance.precedence import PLATFORM_POLICY, merge_policies
from patchfrog.merge_readiness.domain import (
    MergeReadinessDecision,
    MergeReadinessReasonCode,
    MergeReadinessResult,
)

_REPO_ID = uuid.uuid4()


def _base_result(decision: MergeReadinessDecision) -> MergeReadinessResult:
    return MergeReadinessResult(
        decision=decision,
        reason_codes=(MergeReadinessReasonCode.NO_UNRESOLVED_EVIDENCE,) if decision is MergeReadinessDecision.READY else (),
        repository_id=_REPO_ID,
        pull_request_number=1,
        review_run_id=uuid.uuid4(),
        head_sha="a" * 40,
        finding_ids=(),
        limitations=(),
    )


def test_no_policy_requirements_leaves_ready_unchanged() -> None:
    effective = merge_policies(PLATFORM_POLICY, None, None)
    result = apply_policy_to_merge_readiness(_base_result(MergeReadinessDecision.READY), policy=effective)
    assert result.final_decision is MergeReadinessDecision.READY
    assert result.policy_decision.reason_codes == ()


def test_required_verification_absent_downgrades_ready_to_human_review_required() -> None:
    effective = merge_policies(PLATFORM_POLICY, None, None)
    result = apply_policy_to_merge_readiness(
        _base_result(MergeReadinessDecision.READY),
        policy=effective,
        verification=VerificationSignal(required=True, satisfied=False),
    )
    assert result.final_decision is MergeReadinessDecision.HUMAN_REVIEW_REQUIRED
    assert PolicyReasonCode.EXECUTABLE_VERIFICATION_REQUIRED in result.policy_decision.reason_codes


def test_required_verification_present_leaves_ready_unchanged() -> None:
    effective = merge_policies(PLATFORM_POLICY, None, None)
    result = apply_policy_to_merge_readiness(
        _base_result(MergeReadinessDecision.READY),
        policy=effective,
        verification=VerificationSignal(required=True, satisfied=True),
    )
    assert result.final_decision is MergeReadinessDecision.READY
    assert result.policy_decision.reason_codes == ()


def test_policy_can_never_turn_blocked_into_ready() -> None:
    effective = merge_policies(PLATFORM_POLICY, None, None)
    result = apply_policy_to_merge_readiness(_base_result(MergeReadinessDecision.BLOCKED), policy=effective)
    assert result.final_decision is MergeReadinessDecision.BLOCKED


def test_policy_can_never_turn_blocked_into_ready_even_with_satisfied_verification() -> None:
    effective = merge_policies(PLATFORM_POLICY, None, None)
    result = apply_policy_to_merge_readiness(
        _base_result(MergeReadinessDecision.BLOCKED),
        policy=effective,
        verification=VerificationSignal(required=True, satisfied=True),
    )
    assert result.final_decision is MergeReadinessDecision.BLOCKED


def test_policy_can_never_turn_human_review_required_into_ready_without_satisfying_evidence() -> None:
    effective = merge_policies(PLATFORM_POLICY, None, None)
    result = apply_policy_to_merge_readiness(
        _base_result(MergeReadinessDecision.HUMAN_REVIEW_REQUIRED),
        policy=effective,
        verification=VerificationSignal(required=True, satisfied=False),
    )
    assert result.final_decision is MergeReadinessDecision.HUMAN_REVIEW_REQUIRED


def test_org_security_severity_threshold_requires_human_review() -> None:
    org = PolicyRule(scope=PolicyScope.ORGANIZATION, security_requires_human_review_severity_floor=Severity.MEDIUM)
    effective = merge_policies(PLATFORM_POLICY, org, None)
    result = apply_policy_to_merge_readiness(
        _base_result(MergeReadinessDecision.READY),
        policy=effective,
        unresolved_findings=(PolicyRelevantFinding(category=FindingCategory.SECURITY, severity=Severity.MEDIUM),),
    )
    assert result.final_decision is MergeReadinessDecision.HUMAN_REVIEW_REQUIRED
    assert PolicyReasonCode.ORG_POLICY_REQUIRES_HUMAN_REVIEW in result.policy_decision.reason_codes


def test_non_security_finding_never_triggers_security_policy() -> None:
    org = PolicyRule(scope=PolicyScope.ORGANIZATION, security_requires_human_review_severity_floor=Severity.MEDIUM)
    effective = merge_policies(PLATFORM_POLICY, org, None)
    result = apply_policy_to_merge_readiness(
        _base_result(MergeReadinessDecision.READY),
        policy=effective,
        unresolved_findings=(PolicyRelevantFinding(category=FindingCategory.CORRECTNESS, severity=Severity.CRITICAL),),
    )
    assert result.final_decision is MergeReadinessDecision.READY


def test_finding_below_policy_floor_never_triggers() -> None:
    org = PolicyRule(scope=PolicyScope.ORGANIZATION, security_requires_human_review_severity_floor=Severity.MEDIUM)
    effective = merge_policies(PLATFORM_POLICY, org, None)
    result = apply_policy_to_merge_readiness(
        _base_result(MergeReadinessDecision.READY),
        policy=effective,
        unresolved_findings=(PolicyRelevantFinding(category=FindingCategory.SECURITY, severity=Severity.LOW),),
    )
    assert result.final_decision is MergeReadinessDecision.READY


def test_base_result_object_is_never_mutated() -> None:
    base = _base_result(MergeReadinessDecision.READY)
    effective = merge_policies(PLATFORM_POLICY, None, None)
    apply_policy_to_merge_readiness(base, policy=effective, verification=VerificationSignal(required=True, satisfied=False))
    assert base.decision is MergeReadinessDecision.READY
