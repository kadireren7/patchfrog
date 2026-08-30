"""patchfrog.review.effort.ReviewEffortPolicy: deterministic per-candidate
effort tiering (Quality + Cost Guard, Milestone F). Every assertion here
is a pure function of already-known structural/static signals -- no
database, no provider call, matching the test conventions established by
tests/unit/test_review_agent_selection.py and
tests/unit/test_context_adaptive.py."""

from __future__ import annotations

from uuid import uuid4

from patchfrog.analysis.domain import Confidence, FindingCategory, Severity
from patchfrog.review.agents.roles import AgentRole
from patchfrog.review.domain import ReviewCandidate, ReviewCandidateReason, StaticFindingSummary
from patchfrog.review.effort import ReviewEffortDecision, ReviewEffortPolicy
from patchfrog.review.effort_types import CriticExpectation, ReviewEffortReason, ReviewEffortTier

_POLICY = ReviewEffortPolicy()
_MAX_RETRIES = 3


def _candidate(
    *,
    file_path: str = "src/math/add.py",
    symbol_name: str = "add_two_numbers",
    start_line: int = 1,
    end_line: int = 10,
    changed_lines: tuple[int, ...] = (2,),
) -> ReviewCandidate:
    return ReviewCandidate(
        file_path=file_path, symbol_id=None, symbol_name=symbol_name,
        qualified_name=f"src.math.{symbol_name}", start_line=start_line, end_line=end_line,
        changed_lines=changed_lines, static_finding_ids=(), reason=ReviewCandidateReason.CHANGED_SYMBOL,
    )


def _static_finding(
    *, category: FindingCategory = FindingCategory.PERFORMANCE, severity: Severity = Severity.MEDIUM
) -> StaticFindingSummary:
    return StaticFindingSummary(
        finding_id=uuid4(), rule_id="r", category=category, severity=severity,
        confidence=Confidence.MEDIUM, title="t", message="m", start_line=1, end_line=2,
        source_analyzer="ruff",
    )


def _decide(candidate: ReviewCandidate, *, static_findings: tuple[StaticFindingSummary, ...] = ()) -> ReviewEffortDecision:
    return _POLICY.decide_provisional(candidate, static_findings=static_findings, max_retries=_MAX_RETRIES)


# -- Tier classification --------------------------------------------------


def test_simple_candidate_with_no_signal_is_light() -> None:
    decision = _decide(_candidate())
    assert decision.tier is ReviewEffortTier.LIGHT
    assert decision.reasons == (ReviewEffortReason.NO_SIGNAL,)


def test_light_tier_drops_security_when_only_conservative_fallback() -> None:
    decision = _decide(_candidate())
    assert decision.selected_roles == frozenset({AgentRole.CORRECTNESS})


def test_ordinary_single_signal_candidate_is_standard() -> None:
    """One weak, non-high-risk static finding alone -- not routine, but
    not a strong enough signal for DEEP either."""

    decision = _decide(_candidate(), static_findings=(_static_finding(category=FindingCategory.PERFORMANCE),))
    assert decision.tier is ReviewEffortTier.STANDARD
    assert ReviewEffortReason.STATIC_FINDING_PRESENT in decision.reasons


def test_standard_tier_keeps_both_default_roles() -> None:
    decision = _decide(_candidate(), static_findings=(_static_finding(category=FindingCategory.PERFORMANCE),))
    assert decision.selected_roles == frozenset({AgentRole.CORRECTNESS, AgentRole.SECURITY})


def test_security_naming_signal_is_deep() -> None:
    decision = _decide(_candidate(file_path="src/auth/login.py", symbol_name="verify_password"))
    assert decision.tier is ReviewEffortTier.DEEP
    assert ReviewEffortReason.SECURITY_RELEVANT in decision.reasons


def test_static_security_finding_is_deep() -> None:
    decision = _decide(_candidate(), static_findings=(_static_finding(category=FindingCategory.SECURITY),))
    assert decision.tier is ReviewEffortTier.DEEP


def test_high_severity_static_finding_is_deep() -> None:
    decision = _decide(
        _candidate(), static_findings=(_static_finding(category=FindingCategory.PERFORMANCE, severity=Severity.HIGH),)
    )
    assert decision.tier is ReviewEffortTier.DEEP
    assert ReviewEffortReason.STATIC_HIGH_SEVERITY in decision.reasons


def test_high_risk_static_category_is_deep() -> None:
    decision = _decide(
        _candidate(), static_findings=(_static_finding(category=FindingCategory.MEMORY_SAFETY, severity=Severity.LOW),)
    )
    assert decision.tier is ReviewEffortTier.DEEP
    assert ReviewEffortReason.HIGH_RISK_STATIC_CATEGORY in decision.reasons


def test_multiple_weak_structural_signals_together_escalate_to_deep() -> None:
    """Two individually-unremarkable signals (large symbol + many changed
    lines) corroborate each other -- DEEP, even with no security/static
    high-risk signal at all."""

    candidate = _candidate(
        start_line=1, end_line=200, changed_lines=tuple(range(1, 20)),
    )
    decision = _decide(candidate)
    assert decision.tier is ReviewEffortTier.DEEP
    assert ReviewEffortReason.MULTIPLE_STRUCTURAL_SIGNALS in decision.reasons


def test_deep_tier_never_drops_security() -> None:
    decision = _decide(_candidate(), static_findings=(_static_finding(category=FindingCategory.SECURITY),))
    assert AgentRole.SECURITY in decision.selected_roles


def test_reasons_are_deterministic_across_repeated_calls() -> None:
    candidate = _candidate(file_path="src/auth/login.py", symbol_name="verify_password")
    first = _decide(candidate)
    second = _decide(candidate)
    assert first == second


# -- Tier semantics: context/output/critic/retry -------------------------


def test_tier_context_fraction_never_exceeds_one() -> None:
    for candidate, findings in (
        (_candidate(), ()),
        (_candidate(), (_static_finding(category=FindingCategory.PERFORMANCE),)),
        (_candidate(), (_static_finding(category=FindingCategory.SECURITY),)),
    ):
        decision = _decide(candidate, static_findings=findings)
        assert 0.0 < decision.context_token_fraction <= 1.0
        assert 0.0 < decision.per_role_output_token_fraction <= 1.0


def test_light_budget_fractions_are_strictly_smaller_than_standard_and_deep() -> None:
    light = _decide(_candidate())
    standard = _decide(_candidate(), static_findings=(_static_finding(category=FindingCategory.PERFORMANCE),))
    deep = _decide(_candidate(), static_findings=(_static_finding(category=FindingCategory.SECURITY),))

    assert light.tier is ReviewEffortTier.LIGHT
    assert standard.tier is ReviewEffortTier.STANDARD
    assert deep.tier is ReviewEffortTier.DEEP

    assert light.context_token_fraction < standard.context_token_fraction <= deep.context_token_fraction
    assert light.per_role_output_token_fraction < standard.per_role_output_token_fraction <= deep.per_role_output_token_fraction
    assert light.retry_limit <= standard.retry_limit == deep.retry_limit

    # The per-role fraction ordering above is deliberately independent of
    # role count -- verify the *combined* candidate spend it implies
    # never exceeds the configured ceiling for any tier (spec section 7).
    light_role_count, standard_role_count, deep_role_count = 1, 2, 2
    assert light.per_role_output_token_fraction * light_role_count <= 1.0
    assert standard.per_role_output_token_fraction * standard_role_count <= 1.0
    assert deep.per_role_output_token_fraction * deep_role_count <= 1.0


def test_light_disables_adaptive_context() -> None:
    decision = _decide(_candidate())
    assert decision.context_adaptive_enabled is False


def test_standard_and_deep_enable_adaptive_context() -> None:
    standard = _decide(_candidate(), static_findings=(_static_finding(category=FindingCategory.PERFORMANCE),))
    deep = _decide(_candidate(), static_findings=(_static_finding(category=FindingCategory.SECURITY),))
    assert standard.context_adaptive_enabled is True
    assert deep.context_adaptive_enabled is True


def test_critic_expectation_by_tier() -> None:
    light = _decide(_candidate())
    standard = _decide(_candidate(), static_findings=(_static_finding(category=FindingCategory.PERFORMANCE),))
    deep = _decide(_candidate(), static_findings=(_static_finding(category=FindingCategory.SECURITY),))
    assert light.critic_expectation is CriticExpectation.OPTIONAL
    assert standard.critic_expectation is CriticExpectation.SELECTIVE
    assert deep.critic_expectation is CriticExpectation.MANDATORY


def test_retry_limit_never_exceeds_configured_max_retries() -> None:
    for max_retries in (0, 1, 2, 5):
        decision = _POLICY.decide_provisional(_candidate(), static_findings=(), max_retries=max_retries)
        assert decision.retry_limit <= max_retries
        deep_decision = _POLICY.decide_provisional(
            _candidate(), static_findings=(_static_finding(category=FindingCategory.SECURITY),),
            max_retries=max_retries,
        )
        assert deep_decision.retry_limit <= max_retries


# -- Escalation (finalize) -------------------------------------------------


def test_finalize_escalates_standard_to_deep_when_adaptive_expansion_occurred() -> None:
    provisional = _decide(_candidate(), static_findings=(_static_finding(category=FindingCategory.PERFORMANCE),))
    assert provisional.tier is ReviewEffortTier.STANDARD

    final = _POLICY.finalize(provisional, adaptive_expansion_occurred=True, max_retries=_MAX_RETRIES)
    assert final.tier is ReviewEffortTier.DEEP
    assert final.escalated is True
    assert final.escalation_reason is ReviewEffortReason.ADAPTIVE_EXPANSION_OCCURRED
    assert ReviewEffortReason.ADAPTIVE_EXPANSION_OCCURRED in final.reasons
    assert final.critic_expectation is CriticExpectation.MANDATORY


def test_finalize_is_a_noop_when_no_adaptive_expansion_occurred() -> None:
    provisional = _decide(_candidate(), static_findings=(_static_finding(category=FindingCategory.PERFORMANCE),))
    final = _POLICY.finalize(provisional, adaptive_expansion_occurred=False, max_retries=_MAX_RETRIES)
    assert final == provisional
    assert final.escalated is False


def test_finalize_is_a_noop_when_already_deep() -> None:
    provisional = _decide(_candidate(), static_findings=(_static_finding(category=FindingCategory.SECURITY),))
    final = _POLICY.finalize(provisional, adaptive_expansion_occurred=True, max_retries=_MAX_RETRIES)
    assert final == provisional
    assert final.escalated is False


def test_light_tier_can_also_escalate_directly_to_deep() -> None:
    """LIGHT's context policy disables adaptive mode, so in production
    adaptive expansion never actually occurs for a LIGHT candidate -- but
    finalize() itself makes no such assumption; it escalates on the
    evidence it's given, regardless of provisional tier."""

    provisional = _decide(_candidate())
    assert provisional.tier is ReviewEffortTier.LIGHT

    final = _POLICY.finalize(provisional, adaptive_expansion_occurred=True, max_retries=_MAX_RETRIES)
    assert final.tier is ReviewEffortTier.DEEP
    assert final.escalated is True


# -- Provider/model trust boundary --------------------------------------


def test_effort_decision_never_carries_provider_model_or_credentials() -> None:
    """Tier controls execution shape only -- provider, model, critic
    model, and credentials remain exclusively operator-controlled
    (patchfrog.review.runtime_config, Milestone C), structurally absent
    from this decision, not just unused."""

    assert set(ReviewEffortDecision.__dataclass_fields__) == {
        "tier", "reasons", "selected_roles", "context_token_fraction", "context_adaptive_enabled",
        "critic_expectation", "retry_limit", "per_role_output_token_fraction", "escalated", "escalation_reason",
    }
