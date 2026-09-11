"""Z5: Merge Readiness + policy integration.

**Never a second Merge Readiness engine.** This module never imports
or re-implements anything from :mod:`patchfrog.merge_readiness.service`
-- it only ever wraps an already-computed
:class:`~patchfrog.merge_readiness.domain.MergeReadinessResult` (call
``MergeReadinessService.evaluate`` first, exactly as before this
milestone) and may tighten it, never modify V's own domain/service
files. The only *direction* this function can move
:class:`~patchfrog.merge_readiness.domain.MergeReadinessDecision` is
``READY -> HUMAN_REVIEW_REQUIRED`` -- ``BLOCKED`` is never touched
(already the strictest outcome), and nothing here can ever produce
``READY`` from a stricter base decision.
"""

from __future__ import annotations

from dataclasses import dataclass

from patchfrog.analysis.domain import FindingCategory, Severity
from patchfrog.governance.domain import (
    EffectivePolicy,
    PolicyDecision,
    PolicyReasonCode,
    severity_meets_floor,
)
from patchfrog.merge_readiness.domain import MergeReadinessDecision, MergeReadinessResult


@dataclass(frozen=True, slots=True)
class PolicyRelevantFinding:
    """A small, explicit projection of an unresolved finding's category/
    severity -- the caller (which already has the finding rows, having
    just computed ``base_result``) supplies this; this module never
    queries the database itself."""

    category: FindingCategory
    severity: Severity


@dataclass(frozen=True, slots=True)
class VerificationSignal:
    """Whether policy-required executable verification was actually
    attempted and passed for this exact head -- computed by the caller
    from already-persisted verification telemetry (see
    :mod:`patchfrog.executable_verification.telemetry`); never
    re-executes or re-attempts verification itself. **Never fakes
    PASS**: ``satisfied=False`` is the only safe default when unknown."""

    required: bool
    satisfied: bool


#: The default when the caller has no verification-policy question at
#: all -- "not required" is always safe as a default (never "required
#: but satisfied", which would risk a caller accidentally opting into a
#: pass they never actually checked).
_NO_VERIFICATION_REQUIREMENT = VerificationSignal(required=False, satisfied=True)


@dataclass(frozen=True, slots=True)
class PolicyAwareMergeReadinessResult:
    """The base engine result plus policy's own, separately-typed
    tightening decision -- never merged into V's own reason-code enum,
    so V's domain module never needs to know governance exists."""

    base_result: MergeReadinessResult
    policy_decision: PolicyDecision
    final_decision: MergeReadinessDecision


def apply_policy_to_merge_readiness(
    base_result: MergeReadinessResult,
    *,
    policy: EffectivePolicy,
    unresolved_findings: tuple[PolicyRelevantFinding, ...] = (),
    verification: VerificationSignal = _NO_VERIFICATION_REQUIREMENT,
) -> PolicyAwareMergeReadinessResult:
    reasons: list[PolicyReasonCode] = []

    if verification.required and not verification.satisfied:
        reasons.append(PolicyReasonCode.EXECUTABLE_VERIFICATION_REQUIRED)

    if policy.security_requires_human_review_severity_floor is not None:
        floor = policy.security_requires_human_review_severity_floor
        if any(
            f.category is FindingCategory.SECURITY and severity_meets_floor(f.severity, floor)
            for f in unresolved_findings
        ):
            reasons.append(PolicyReasonCode.ORG_POLICY_REQUIRES_HUMAN_REVIEW)

    if policy.security_block_severity_floor is not None:
        floor = policy.security_block_severity_floor
        if any(
            f.category is FindingCategory.SECURITY and severity_meets_floor(f.severity, floor)
            for f in unresolved_findings
        ):
            reasons.append(PolicyReasonCode.REQUIRED_SECURITY_REVIEW_MISSING)

    final_decision = base_result.decision
    if reasons and final_decision is MergeReadinessDecision.READY:
        # The only direction this function is structurally capable of
        # moving the decision: strictly more restrictive. BLOCKED is
        # never reachable here (never assigned), and READY is never
        # produced from anything other than "no reasons and base_result
        # was already READY".
        final_decision = MergeReadinessDecision.HUMAN_REVIEW_REQUIRED

    return PolicyAwareMergeReadinessResult(
        base_result=base_result, policy_decision=PolicyDecision(reason_codes=tuple(reasons)), final_decision=final_decision
    )
