"""patchfrog.review.agents.cross_role: same-root-cause merge detection
and contradiction detection/resolution across specialist roles.

Covers Agent Orchestration v1 spec sections 6, 9, 10 and required test
scenarios 4, 5, 6, 7, 8."""

from __future__ import annotations

from patchfrog.analysis.domain import Confidence, FindingCategory, Severity
from patchfrog.review.agents.cross_role import (
    CROSS_ROLE_DUPLICATE,
    UNRESOLVED_CONTRADICTION,
    group_cross_role,
    is_contradiction,
    is_same_root_cause,
    preferred,
    resolve_unresolved_contradictions,
)
from patchfrog.review.agents.proposal import AgentProposal
from patchfrog.review.agents.roles import AgentRole
from patchfrog.review.domain import (
    AIReviewFinding,
    CriticDecision,
    CriticVerdict,
    ReviewEvidence,
    ValidatedFinding,
    ValidationOutcome,
)


def _finding(
    *,
    title: str = "bug",
    message: str = "msg",
    reasoning_summary: str = "reason",
    category: FindingCategory = FindingCategory.CORRECTNESS,
    severity: Severity = Severity.MEDIUM,
    confidence: Confidence = Confidence.MEDIUM,
    file_path: str = "src/billing.py",
    start_line: int = 10,
    end_line: int = 12,
    quoted_text: str = "shared_evidence_line",
    impact: str | None = None,
) -> AIReviewFinding:
    return AIReviewFinding(
        title=title, message=message, category=category, severity=severity, confidence=confidence,
        file_path=file_path, start_line=start_line, end_line=end_line,
        evidence=(ReviewEvidence(file_path=file_path, start_line=start_line, end_line=end_line, quoted_text=quoted_text),),
        reasoning_summary=reasoning_summary, impact=impact,
    )


def _proposal(
    role: AgentRole, finding: AIReviewFinding, *, critic_verdict: CriticVerdict | None = None
) -> AgentProposal:
    return AgentProposal(
        role=role,
        validated=ValidatedFinding(finding=finding, outcome=ValidationOutcome.VALID, detail=""),
        critic_verdict=critic_verdict,
    )


def test_same_category_overlapping_is_same_root_cause() -> None:
    a = _proposal(AgentRole.CORRECTNESS, _finding(category=FindingCategory.CORRECTNESS))
    b = _proposal(AgentRole.SECURITY, _finding(category=FindingCategory.CORRECTNESS))
    assert is_same_root_cause(a, b) is True


def test_different_category_shared_evidence_is_same_root_cause() -> None:
    """The module docstring's own worked example: correctness says
    'shell argument construction is incorrect', security says 'untrusted
    value reaches shell invocation' -- same underlying code, different
    framing."""

    a = _proposal(AgentRole.CORRECTNESS, _finding(category=FindingCategory.CORRECTNESS, quoted_text="shell_cmd"))
    b = _proposal(AgentRole.SECURITY, _finding(category=FindingCategory.SECURITY, quoted_text="shell_cmd"))
    assert is_same_root_cause(a, b) is True


def test_different_category_no_shared_evidence_is_not_same_root_cause() -> None:
    """Required scenario 6: two genuinely different findings at the same
    location -- both may survive."""

    a = _proposal(AgentRole.CORRECTNESS, _finding(category=FindingCategory.CORRECTNESS, quoted_text="line_a"))
    b = _proposal(AgentRole.SECURITY, _finding(category=FindingCategory.SECURITY, quoted_text="line_b"))
    assert is_same_root_cause(a, b) is False


def test_non_overlapping_location_is_not_same_root_cause() -> None:
    a = _proposal(AgentRole.CORRECTNESS, _finding(start_line=10, end_line=12))
    b = _proposal(AgentRole.SECURITY, _finding(start_line=100, end_line=105))
    assert is_same_root_cause(a, b) is False


def test_same_role_pair_is_never_cross_role_merged() -> None:
    a = _proposal(AgentRole.CORRECTNESS, _finding(title="a"))
    b = _proposal(AgentRole.CORRECTNESS, _finding(title="b"))
    assert is_same_root_cause(a, b) is False


def test_preferred_prefers_security_categorization_when_evidence_overlaps() -> None:
    correctness = _proposal(
        AgentRole.CORRECTNESS,
        _finding(category=FindingCategory.CORRECTNESS, severity=Severity.MEDIUM, confidence=Confidence.MEDIUM),
    )
    security = _proposal(
        AgentRole.SECURITY,
        _finding(category=FindingCategory.SECURITY, severity=Severity.MEDIUM, confidence=Confidence.MEDIUM),
    )
    assert preferred(correctness, security) is security
    assert preferred(security, correctness) is security


def test_preferred_prefers_higher_severity_when_not_security_correctness_pair() -> None:
    low = _proposal(AgentRole.CORRECTNESS, _finding(category=FindingCategory.CORRECTNESS, severity=Severity.LOW))
    high = _proposal(
        AgentRole.SECURITY, _finding(category=FindingCategory.CORRECTNESS, severity=Severity.HIGH)
    )
    assert preferred(low, high) is high


def test_preferred_is_deterministic_tie_break_when_all_else_equal() -> None:
    a = _proposal(AgentRole.CORRECTNESS, _finding(title="same", category=FindingCategory.CORRECTNESS))
    b = _proposal(AgentRole.SECURITY, _finding(title="same", category=FindingCategory.CORRECTNESS))
    assert preferred(a, b) is preferred(a, b)  # stable across repeated calls
    assert preferred(a, b) is a  # "correctness" < "security" lexically


def test_contradiction_detected_for_opposite_safety_claims() -> None:
    """Required scenario 7 setup: the module docstring's own contradiction
    example -- one side claims unsanitized/unsafe, the other claims a
    sanitizer guarantees safety, over the same shared evidence."""

    security = _proposal(
        AgentRole.SECURITY,
        _finding(
            category=FindingCategory.SECURITY, message="input is unsanitized before reaching the query",
            reasoning_summary="untrusted value flows to the sink", quoted_text="shared_line",
        ),
    )
    correctness = _proposal(
        AgentRole.CORRECTNESS,
        _finding(
            category=FindingCategory.CORRECTNESS, message="the sanitizer guarantees this value is safe",
            reasoning_summary="already validated upstream", quoted_text="shared_line",
        ),
    )
    assert is_contradiction(security, correctness) is True
    assert is_contradiction(correctness, security) is True


def test_contradiction_requires_shared_evidence() -> None:
    security = _proposal(
        AgentRole.SECURITY,
        _finding(message="input is unsanitized", quoted_text="line_a", start_line=10, end_line=12),
    )
    correctness = _proposal(
        AgentRole.CORRECTNESS,
        _finding(message="this value is safe and sanitized", quoted_text="line_b", start_line=10, end_line=12),
    )
    assert is_contradiction(security, correctness) is False


def test_agreement_is_not_a_contradiction() -> None:
    a = _proposal(AgentRole.CORRECTNESS, _finding(message="a bug"))
    b = _proposal(AgentRole.SECURITY, _finding(message="a bug"))
    assert is_contradiction(a, b) is False


def test_group_cross_role_merges_exact_duplicate_to_one_survivor() -> None:
    """Required scenario 4: exact duplicate across agents -> one result."""

    a = _proposal(AgentRole.CORRECTNESS, _finding(category=FindingCategory.CORRECTNESS))
    b = _proposal(AgentRole.SECURITY, _finding(category=FindingCategory.CORRECTNESS))
    result = group_cross_role((a, b))
    suppressed = [p for p in result.proposals if p.suppressed_reason == CROSS_ROLE_DUPLICATE]
    survivors = [p for p in result.proposals if p.suppressed_reason is None]
    assert len(suppressed) == 1
    assert len(survivors) == 1


def test_group_cross_role_keeps_deterministic_winner_for_overlapping_finding() -> None:
    """Required scenario 5: overlapping same-root-cause finding ->
    deterministic winner (security's categorization, per the shared-evidence
    example)."""

    correctness = _proposal(
        AgentRole.CORRECTNESS,
        _finding(category=FindingCategory.CORRECTNESS, quoted_text="shell_cmd", title="shell arg construction wrong"),
    )
    security = _proposal(
        AgentRole.SECURITY,
        _finding(category=FindingCategory.SECURITY, quoted_text="shell_cmd", title="untrusted value reaches shell"),
    )
    result = group_cross_role((correctness, security))
    survivors = [p for p in result.proposals if p.suppressed_reason is None]
    assert len(survivors) == 1
    assert survivors[0].role is AgentRole.SECURITY


def test_group_cross_role_keeps_both_genuinely_different_findings() -> None:
    """Required scenario 6."""

    a = _proposal(AgentRole.CORRECTNESS, _finding(category=FindingCategory.CORRECTNESS, quoted_text="line_a"))
    b = _proposal(AgentRole.SECURITY, _finding(category=FindingCategory.SECURITY, quoted_text="line_b"))
    result = group_cross_role((a, b))
    survivors = [p for p in result.proposals if p.suppressed_reason is None]
    assert len(survivors) == 2
    assert not result.contradiction_indices


def test_group_cross_role_flags_contradiction_indices() -> None:
    """Required scenario 7: contradictory proposals must be flagged for
    forced critique, never silently merged or silently kept both."""

    security = _proposal(
        AgentRole.SECURITY, _finding(message="input is unsanitized before use", quoted_text="shared")
    )
    correctness = _proposal(
        AgentRole.CORRECTNESS, _finding(message="already sanitized and safe here", quoted_text="shared")
    )
    result = group_cross_role((security, correctness))
    assert result.contradiction_indices == frozenset({0, 1})
    # Never suppressed by the grouping step itself -- suppression (if any)
    # only happens after critique, via resolve_unresolved_contradictions.
    assert all(p.suppressed_reason is None for p in result.proposals)


def test_invalid_proposals_are_never_grouped() -> None:
    invalid = AgentProposal(
        role=AgentRole.CORRECTNESS,
        validated=ValidatedFinding(finding=_finding(), outcome=ValidationOutcome.HALLUCINATED_EVIDENCE, detail="x"),
    )
    valid = _proposal(AgentRole.SECURITY, _finding())
    result = group_cross_role((invalid, valid))
    assert result.proposals[0].suppressed_reason is None
    assert result.proposals[1].suppressed_reason is None
    assert not result.contradiction_indices


def test_resolve_unresolved_contradictions_suppresses_when_neither_rejected() -> None:
    """Required scenario 8: unresolved contradiction -> safe suppression
    (critic could not confidently resolve which claim is correct)."""

    a = _proposal(
        AgentRole.SECURITY, _finding(title="a"),
        critic_verdict=CriticVerdict(decision=CriticDecision.ACCEPT, reasoning_summary="ok"),
    )
    b = _proposal(
        AgentRole.CORRECTNESS, _finding(title="b"),
        critic_verdict=CriticVerdict(decision=CriticDecision.ACCEPT, reasoning_summary="ok"),
    )
    result = resolve_unresolved_contradictions((a, b), contradiction_indices=frozenset({0, 1}))
    assert all(p.suppressed_reason == UNRESOLVED_CONTRADICTION for p in result)


def test_resolve_unresolved_contradictions_keeps_survivor_when_critic_rejects_one() -> None:
    a = _proposal(
        AgentRole.SECURITY, _finding(title="a"),
        critic_verdict=CriticVerdict(decision=CriticDecision.ACCEPT, reasoning_summary="ok"),
    )
    b = _proposal(
        AgentRole.CORRECTNESS, _finding(title="b"),
        critic_verdict=CriticVerdict(decision=CriticDecision.REJECT, reasoning_summary="not supported"),
    )
    result = resolve_unresolved_contradictions((a, b), contradiction_indices=frozenset({0, 1}))
    assert result[0].suppressed_reason is None
    assert result[1].suppressed_reason is None  # rejected proposals are handled by the normal REJECTED_CRITIC path


def test_resolve_unresolved_contradictions_no_op_without_contradiction_indices() -> None:
    a = _proposal(AgentRole.SECURITY, _finding())
    result = resolve_unresolved_contradictions((a,), contradiction_indices=frozenset())
    assert result == (a,)
