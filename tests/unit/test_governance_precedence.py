"""Z1/Z6 precedence-merge corpus -- platform floor cannot be weakened,
org policy inherited, repo may tighten but never relax, effective
policy is deterministic (spec tests Z1-Z4, Z8, Z22)."""

from __future__ import annotations

from patchfrog.analysis.domain import FindingCategory, Severity
from patchfrog.governance.domain import PolicyRule, PolicyScope
from patchfrog.governance.precedence import PLATFORM_POLICY, merge_policies


def test_platform_floor_alone_is_the_effective_policy_when_nothing_else_is_set() -> None:
    effective = merge_policies(PLATFORM_POLICY, None, None)
    assert effective.security_block_severity_floor == PLATFORM_POLICY.security_block_severity_floor
    assert effective.security_suppression_forbidden is True
    assert effective.contributing_scopes == (PolicyScope.PLATFORM,)


def test_org_policy_is_inherited_when_no_repository_override_exists() -> None:
    org = PolicyRule(scope=PolicyScope.ORGANIZATION, security_requires_human_review_severity_floor=Severity.MEDIUM)
    effective = merge_policies(PLATFORM_POLICY, org, None)
    assert effective.security_requires_human_review_severity_floor == Severity.MEDIUM
    assert effective.contributing_scopes == (PolicyScope.PLATFORM, PolicyScope.ORGANIZATION)


def test_repository_can_tighten_severity_floor_beyond_org() -> None:
    org = PolicyRule(scope=PolicyScope.ORGANIZATION, security_requires_human_review_severity_floor=Severity.HIGH)
    repo = PolicyRule(scope=PolicyScope.REPOSITORY, security_requires_human_review_severity_floor=Severity.LOW)
    effective = merge_policies(PLATFORM_POLICY, org, repo)
    # LOW is a stricter (broader-catching) floor than HIGH.
    assert effective.security_requires_human_review_severity_floor == Severity.LOW


def test_repository_cannot_relax_a_stricter_org_severity_floor() -> None:
    org = PolicyRule(scope=PolicyScope.ORGANIZATION, security_requires_human_review_severity_floor=Severity.LOW)
    repo = PolicyRule(scope=PolicyScope.REPOSITORY, security_requires_human_review_severity_floor=Severity.HIGH)
    effective = merge_policies(PLATFORM_POLICY, org, repo)
    # Repo tried to relax to HIGH; org's stricter LOW still wins.
    assert effective.security_requires_human_review_severity_floor == Severity.LOW


def test_repository_cannot_widen_the_platform_allowed_providers() -> None:
    platform_with_allowlist = PolicyRule(scope=PolicyScope.PLATFORM, allowed_providers=frozenset({"anthropic", "openai"}))
    repo = PolicyRule(scope=PolicyScope.REPOSITORY, allowed_providers=frozenset({"anthropic", "openai", "gemini"}))
    effective = merge_policies(platform_with_allowlist, None, repo)
    # Intersection -- gemini was never allowed by platform, so it never appears.
    assert effective.allowed_providers == frozenset({"anthropic", "openai"})


def test_repository_can_narrow_allowed_providers() -> None:
    platform_with_allowlist = PolicyRule(scope=PolicyScope.PLATFORM, allowed_providers=frozenset({"anthropic", "openai"}))
    repo = PolicyRule(scope=PolicyScope.REPOSITORY, allowed_providers=frozenset({"anthropic"}))
    effective = merge_policies(platform_with_allowlist, None, repo)
    assert effective.allowed_providers == frozenset({"anthropic"})


def test_verification_requirements_are_cumulative_union_never_removed() -> None:
    org = PolicyRule(scope=PolicyScope.ORGANIZATION, verification_required_categories=frozenset({FindingCategory.SECURITY}))
    repo = PolicyRule(scope=PolicyScope.REPOSITORY, verification_required_path_prefixes=frozenset({"auth/"}))
    effective = merge_policies(PLATFORM_POLICY, org, repo)
    assert effective.verification_required_categories == frozenset({FindingCategory.SECURITY})
    assert effective.verification_required_path_prefixes == frozenset({"auth/"})


def test_any_scope_disabling_personalization_wins() -> None:
    org = PolicyRule(scope=PolicyScope.ORGANIZATION, personalization_enabled=True)
    repo = PolicyRule(scope=PolicyScope.REPOSITORY, personalization_enabled=False)
    effective = merge_policies(PLATFORM_POLICY, org, repo)
    assert effective.personalization_enabled is False


def test_security_suppression_forbidden_cannot_be_turned_off_by_a_laxer_scope() -> None:
    # PLATFORM_POLICY already forbids it; a repo trying to allow it must not succeed.
    repo = PolicyRule(scope=PolicyScope.REPOSITORY, security_suppression_forbidden=False)
    effective = merge_policies(PLATFORM_POLICY, None, repo)
    assert effective.security_suppression_forbidden is True


def test_effective_policy_is_deterministic_for_identical_inputs() -> None:
    org = PolicyRule(scope=PolicyScope.ORGANIZATION, security_requires_human_review_severity_floor=Severity.MEDIUM)
    repo = PolicyRule(scope=PolicyScope.REPOSITORY, verification_required_categories=frozenset({FindingCategory.SECURITY}))
    first = merge_policies(PLATFORM_POLICY, org, repo)
    second = merge_policies(PLATFORM_POLICY, org, repo)
    assert first == second


def test_reason_codes_are_typed_members_of_a_stable_enum() -> None:
    from patchfrog.governance.domain import PolicyReasonCode

    # Every value is a plain, stable string -- never freeform prose.
    for code in PolicyReasonCode:
        assert code.value == code.value.lower()
        assert " " not in code.value
