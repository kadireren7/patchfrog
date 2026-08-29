"""patchfrog.review.agents.selection: deterministic specialist-role
selection. Correctness is always selected; Security's selection reason
must reflect the actual signal that triggered it."""

from __future__ import annotations

from uuid import uuid4

from patchfrog.analysis.domain import Confidence, FindingCategory, Severity
from patchfrog.review.agents.roles import AgentRole
from patchfrog.review.agents.selection import AgentSelectionPolicy, AgentSelectionReason
from patchfrog.review.domain import ReviewCandidate, ReviewCandidateReason, StaticFindingSummary

_POLICY = AgentSelectionPolicy()


def _candidate(*, file_path: str = "src/billing.py", symbol_name: str = "helper") -> ReviewCandidate:
    return ReviewCandidate(
        file_path=file_path, symbol_id=None, symbol_name=symbol_name,
        qualified_name=f"src.billing.{symbol_name}", start_line=1, end_line=10,
        changed_lines=(2,), static_finding_ids=(), reason=ReviewCandidateReason.CHANGED_SYMBOL,
    )


def _static_finding(category: FindingCategory) -> StaticFindingSummary:
    return StaticFindingSummary(
        finding_id=uuid4(), rule_id="r", category=category, severity=Severity.MEDIUM,
        confidence=Confidence.MEDIUM, title="t", message="m", start_line=1, end_line=2,
        source_analyzer="ruff",
    )


def test_correctness_always_selected() -> None:
    decisions = _POLICY.select(_candidate(), static_findings=())
    roles = {d.role for d in decisions}
    assert AgentRole.CORRECTNESS in roles


def test_correctness_reason_is_default() -> None:
    decisions = _POLICY.select(_candidate(), static_findings=())
    correctness = next(d for d in decisions if d.role is AgentRole.CORRECTNESS)
    assert correctness.reason == AgentSelectionReason.DEFAULT


def test_security_selected_via_static_corroboration() -> None:
    decisions = _POLICY.select(_candidate(), static_findings=(_static_finding(FindingCategory.SECURITY),))
    security = next(d for d in decisions if d.role is AgentRole.SECURITY)
    assert security.reason == AgentSelectionReason.STATIC_SECURITY_CORROBORATION


def test_security_selected_via_naming_heuristic() -> None:
    decisions = _POLICY.select(
        _candidate(file_path="src/auth/login.py", symbol_name="verify_password"), static_findings=()
    )
    security = next(d for d in decisions if d.role is AgentRole.SECURITY)
    assert security.reason == AgentSelectionReason.SECURITY_SENSITIVE_NAMING


def test_security_selected_via_conservative_fallback() -> None:
    decisions = _POLICY.select(
        _candidate(file_path="src/math/add.py", symbol_name="add_two_numbers"), static_findings=()
    )
    security = next(d for d in decisions if d.role is AgentRole.SECURITY)
    assert security.reason == AgentSelectionReason.CONSERVATIVE_FALLBACK


def test_non_security_static_finding_does_not_trigger_corroboration_reason() -> None:
    decisions = _POLICY.select(
        _candidate(file_path="src/math/add.py", symbol_name="add_two_numbers"),
        static_findings=(_static_finding(FindingCategory.PERFORMANCE),),
    )
    security = next(d for d in decisions if d.role is AgentRole.SECURITY)
    assert security.reason != AgentSelectionReason.STATIC_SECURITY_CORROBORATION


def test_selection_is_deterministic_across_repeated_calls() -> None:
    candidate = _candidate()
    first = _POLICY.select(candidate, static_findings=())
    second = _POLICY.select(candidate, static_findings=())
    assert first == second
