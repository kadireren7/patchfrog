"""patchfrog.review.critic_selection.CriticSelectionPolicy: deterministic
critic verification selection. The policy only ever *skips* the critic
in two narrow, explainable cases -- everything else, including any
cross-role overlap, still gets verified."""

from __future__ import annotations

from patchfrog.analysis.domain import Confidence, FindingCategory, Severity
from patchfrog.review.agents.roles import AgentRole
from patchfrog.review.critic_selection import CriticSelectionInput, CriticSelectionPolicy

_POLICY = CriticSelectionPolicy()


def _input(
    *,
    role: AgentRole = AgentRole.CORRECTNESS,
    category: FindingCategory = FindingCategory.CORRECTNESS,
    severity: Severity = Severity.MEDIUM,
    confidence: Confidence = Confidence.HIGH,
    corroborated_by_static: bool = True,
    file_path: str = "src/billing.py",
    start_line: int = 10,
    end_line: int = 12,
) -> CriticSelectionInput:
    return CriticSelectionInput(
        role=role, category=category, severity=severity, confidence=confidence,
        corroborated_by_static=corroborated_by_static, file_path=file_path,
        start_line=start_line, end_line=end_line,
    )


def test_low_risk_corroborated_proposal_is_skipped() -> None:
    """The one case this policy actually skips: HIGH confidence,
    non-security, non-HIGH/CRITICAL severity, static-corroborated, no
    cross-role overlap."""

    target = _input(severity=Severity.LOW, confidence=Confidence.HIGH, corroborated_by_static=True)
    assert _POLICY.should_critique(target, peers=(), min_final_confidence=Confidence.LOW) is False


def test_high_severity_always_critiqued() -> None:
    target = _input(severity=Severity.HIGH, confidence=Confidence.HIGH, corroborated_by_static=True)
    assert _POLICY.should_critique(target, peers=(), min_final_confidence=Confidence.LOW) is True


def test_critical_severity_always_critiqued() -> None:
    target = _input(severity=Severity.CRITICAL, confidence=Confidence.HIGH, corroborated_by_static=True)
    assert _POLICY.should_critique(target, peers=(), min_final_confidence=Confidence.LOW) is True


def test_security_category_always_critiqued() -> None:
    target = _input(
        category=FindingCategory.SECURITY, severity=Severity.LOW, confidence=Confidence.HIGH,
        corroborated_by_static=True,
    )
    assert _POLICY.should_critique(target, peers=(), min_final_confidence=Confidence.LOW) is True


def test_low_confidence_always_critiqued() -> None:
    target = _input(severity=Severity.LOW, confidence=Confidence.LOW, corroborated_by_static=True)
    assert _POLICY.should_critique(target, peers=(), min_final_confidence=Confidence.LOW) is True


def test_medium_confidence_always_critiqued() -> None:
    target = _input(severity=Severity.LOW, confidence=Confidence.MEDIUM, corroborated_by_static=True)
    assert _POLICY.should_critique(target, peers=(), min_final_confidence=Confidence.LOW) is True


def test_not_corroborated_always_critiqued() -> None:
    target = _input(severity=Severity.LOW, confidence=Confidence.HIGH, corroborated_by_static=False)
    assert _POLICY.should_critique(target, peers=(), min_final_confidence=Confidence.LOW) is True


def test_overlapping_cross_role_peer_forces_critique() -> None:
    target = _input(
        role=AgentRole.CORRECTNESS, severity=Severity.LOW, confidence=Confidence.HIGH,
        corroborated_by_static=True, file_path="src/billing.py", start_line=10, end_line=12,
    )
    peer = _input(role=AgentRole.SECURITY, file_path="src/billing.py", start_line=11, end_line=13)
    assert _POLICY.should_critique(target, peers=(peer,), min_final_confidence=Confidence.LOW) is True


def test_non_overlapping_peer_does_not_force_critique() -> None:
    target = _input(severity=Severity.LOW, confidence=Confidence.HIGH, corroborated_by_static=True)
    peer = _input(role=AgentRole.SECURITY, file_path="src/other.py", start_line=100, end_line=105)
    assert _POLICY.should_critique(target, peers=(peer,), min_final_confidence=Confidence.LOW) is False


def test_guaranteed_below_threshold_is_skipped_even_if_otherwise_risky() -> None:
    """A LOW confidence, non-corroborated proposal against a HIGH
    min_final_confidence bar can never reach HIGH even with the best-case
    +1 corroboration boost -- skip regardless of the other rules."""

    target = _input(
        severity=Severity.HIGH,  # would otherwise force critique
        confidence=Confidence.LOW, corroborated_by_static=False,
    )
    assert _POLICY.should_critique(target, peers=(), min_final_confidence=Confidence.HIGH) is False


def test_corroboration_boost_can_still_reach_threshold() -> None:
    """MEDIUM confidence + corroboration boosts the best case to HIGH,
    which meets a HIGH bar -- not guaranteed below threshold, so the
    remaining rules apply (MEDIUM confidence forces critique here)."""

    target = _input(severity=Severity.LOW, confidence=Confidence.MEDIUM, corroborated_by_static=True)
    assert _POLICY.should_critique(target, peers=(), min_final_confidence=Confidence.HIGH) is True
