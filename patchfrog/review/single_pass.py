"""M4 single-pass review: deterministic batching, finding attribution and
escalation decisions.

The provider call itself lives in
:meth:`patchfrog.review.orchestration.AgentOrchestrator.review_batch`
(so it shares the exact retry / one-hop fallback / budget-reservation
machinery of the specialist path). This module holds the pure,
provider-free decisions around it:

- :func:`plan_batches` -- which candidates share one provider call;
- :func:`attribute_finding` -- which candidate a returned finding
  belongs to (for persistence, dedup and critic verification);
- :func:`decide_escalations` -- whether any *additional* specialist call
  is justified, and why. Every extra call carries an
  :class:`~patchfrog.review.cost_policy.EscalationReason`; nothing
  escalates speculatively, and escalation never runs concurrently with
  the first pass (sequential by construction).
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from patchfrog.analysis.domain import FindingCategory, Severity
from patchfrog.change_risk import (
    ChangeRiskClassification,
    ChangeRiskSignal,
    ChangeRiskTier,
    FileChangeClass,
)
from patchfrog.review.agents.evidence import CandidateEvidencePackage
from patchfrog.review.agents.roles import AgentRole
from patchfrog.review.cost_policy import EscalationReason
from patchfrog.review.domain import AIReviewFinding, TokenUsage, ValidatedFinding, ValidationOutcome


@dataclass(frozen=True, slots=True)
class BatchTarget:
    """One prepared candidate inside a batched call. ``key`` is the
    caller's stable identifier for the candidate (its index in the run)."""

    key: int
    evidence: CandidateEvidencePackage


@dataclass(slots=True)
class BatchCallResult:
    role: AgentRole
    #: Validated findings attributed to each target ``key``.
    per_target: dict[int, list[ValidatedFinding]] = field(default_factory=dict)
    usage: TokenUsage = field(default_factory=TokenUsage)
    retries_used: int = 0
    latency_ms: float = 0.0
    used_fallback: bool = False
    served_by: str | None = None
    #: A provider call was actually attempted (0/1 per batch).
    attempted: bool = False
    failed: bool = False
    budget_exhausted: bool = False
    error: str | None = None
    #: Estimated prompt size, safe to record (a count, never text).
    estimated_input_tokens: int = 0


@dataclass(frozen=True, slots=True)
class EscalationPlan:
    role: AgentRole
    reason: EscalationReason
    target_keys: tuple[int, ...]


def dedupe_context_blocks(targets: Sequence[BatchTarget]) -> tuple[str, ...]:
    """Every distinct context block once, in first-seen order -- the
    context-minimization half of batching: two candidates in the same
    file share their containing-module context instead of paying for it
    twice."""

    seen: set[str] = set()
    blocks: list[str] = []
    for target in targets:
        candidate_blocks = target.evidence.context_blocks or (
            (target.evidence.context_text,) if target.evidence.context_text.strip() else ()
        )
        for block in candidate_blocks:
            if block not in seen:
                seen.add(block)
                blocks.append(block)
    return tuple(blocks)


def plan_batches(
    targets: Sequence[BatchTarget],
    *,
    estimate: Callable[[Sequence[BatchTarget]], int],
    max_input_tokens: int,
) -> list[list[BatchTarget]]:
    """Greedy, order-preserving batching: extend the current batch while
    its estimated prompt stays within ``max_input_tokens``. A single
    target larger than the limit still gets its own batch (never
    dropped). Deterministic for a given input order."""

    batches: list[list[BatchTarget]] = []
    current: list[BatchTarget] = []
    for target in targets:
        if current and estimate([*current, target]) > max_input_tokens:
            batches.append(current)
            current = []
        current.append(target)
    if current:
        batches.append(current)
    return batches


def _overlap(finding: AIReviewFinding, start: int, end: int) -> int:
    return max(0, min(finding.end_line, end) - max(finding.start_line, start) + 1)


def attribute_finding(finding: AIReviewFinding, targets: Sequence[BatchTarget]) -> int:
    """Deterministic owner for one returned finding:

    1. a target in the same file whose span overlaps the finding the most;
    2. otherwise the nearest target in the same file;
    3. otherwise the first target that was shown the finding's file as
       context;
    4. otherwise the first target (validation will already have marked
       such a finding OUT_OF_SCOPE -- it is persisted for audit only).
    """

    same_file = [t for t in targets if t.evidence.candidate.file_path == finding.file_path]
    if same_file:
        best = max(
            same_file,
            key=lambda t: (
                _overlap(finding, t.evidence.candidate.start_line, t.evidence.candidate.end_line),
                -min(
                    abs(finding.start_line - t.evidence.candidate.start_line),
                    abs(finding.start_line - t.evidence.candidate.end_line),
                ),
                -t.key,
            ),
        )
        return best.key
    for t in targets:
        if finding.file_path in t.evidence.allowed_file_paths:
            return t.key
    return targets[0].key


_HIGH_SEVERITIES = (Severity.HIGH, Severity.CRITICAL)


def decide_escalations(
    classification: ChangeRiskClassification,
    *,
    targets: Sequence[BatchTarget],
    first_pass: dict[int, list[ValidatedFinding]],
    remaining_provider_calls: int,
) -> list[EscalationPlan]:
    """Sequential specialist escalation after the single pass.

    Only ELEVATED/HIGH_RISK runs may escalate, each escalation needs a
    typed reason grounded in a deterministic signal, and an escalation is
    only planned while at least one provider call would *still* remain
    for critic verification afterwards (an escalation that would starve
    the critic is worse than none). Security is considered before
    Correctness. HIGH_RISK may plan both, but a Correctness escalation
    only covers candidates the Security escalation does not already
    re-review (the single pass already covered correctness for them);
    ELEVATED plans at most one.
    """

    if classification.tier not in (ChangeRiskTier.ELEVATED, ChangeRiskTier.HIGH_RISK):
        return []
    max_escalations = 2 if classification.tier is ChangeRiskTier.HIGH_RISK else 1
    plans: list[EscalationPlan] = []
    budget = remaining_provider_calls

    def can_afford() -> bool:
        return len(plans) < max_escalations and budget - 1 >= 1

    signals = set(classification.signals)
    security_paths = set(classification.security_sensitive_paths)

    security_reason: EscalationReason | None = None
    security_keys: tuple[int, ...] = ()
    first_pass_security = tuple(
        key
        for key, findings in sorted(first_pass.items())
        if any(
            v.outcome is ValidationOutcome.VALID
            and (v.finding.category is FindingCategory.SECURITY or v.finding.severity in _HIGH_SEVERITIES)
            for v in findings
        )
    )
    if ChangeRiskSignal.STATIC_HIGH_RISK_FINDING in signals:
        security_reason = EscalationReason.STATIC_HIGH_RISK_FINDING
        security_keys = tuple(t.key for t in targets if t.evidence.static_findings) or tuple(t.key for t in targets)
    elif security_paths:
        security_reason = EscalationReason.SECURITY_SENSITIVE_CHANGE
        security_keys = tuple(t.key for t in targets if t.evidence.candidate.file_path in security_paths)
    elif ChangeRiskSignal.CI_CONFIG_CHANGE in signals:
        ci_paths = set(classification.paths_with(FileChangeClass.CI))
        security_reason = EscalationReason.CI_CONFIG_CHANGE
        security_keys = tuple(t.key for t in targets if t.evidence.candidate.file_path in ci_paths)
    elif first_pass_security:
        security_reason = EscalationReason.HIGH_RISK_FIRST_PASS_FINDING
        security_keys = first_pass_security
    if security_reason is not None and security_keys and can_afford():
        plans.append(EscalationPlan(AgentRole.SECURITY, security_reason, security_keys))
        budget -= 1

    if classification.tier is ChangeRiskTier.HIGH_RISK:
        correctness_reason: EscalationReason | None = None
        contract_paths: set[str] = set()
        if ChangeRiskSignal.DATABASE_MIGRATION in signals:
            correctness_reason = EscalationReason.DATABASE_MIGRATION
            contract_paths = set(classification.paths_with(FileChangeClass.MIGRATION))
        elif ChangeRiskSignal.PUBLIC_INTERFACE_CHANGE in signals or ChangeRiskSignal.SCHEMA_MODEL_CHANGE in signals:
            correctness_reason = EscalationReason.PUBLIC_CONTRACT_CHANGE
            contract_paths = set(classification.public_interface_paths) | set(
                classification.paths_with(FileChangeClass.SCHEMA)
            )
        elif ChangeRiskSignal.CROSS_MODULE in signals:
            correctness_reason = EscalationReason.CROSS_MODULE_COMPLEXITY
        if correctness_reason is not None and can_afford():
            keys = tuple(t.key for t in targets if t.evidence.candidate.file_path in contract_paths) or tuple(
                t.key for t in targets
            )
            # The single pass already reviewed correctness/contracts; a
            # second specialist call is only worth paying for when it
            # reaches candidates the security escalation does not already
            # re-review.
            already = set(plans[0].target_keys) if plans else set()
            keys = tuple(k for k in keys if k not in already)
            if keys:
                plans.append(EscalationPlan(AgentRole.CORRECTNESS, correctness_reason, keys))
                budget -= 1
    return plans


__all__ = [
    "BatchCallResult",
    "BatchTarget",
    "EscalationPlan",
    "attribute_finding",
    "decide_escalations",
    "dedupe_context_blocks",
    "plan_batches",
]
