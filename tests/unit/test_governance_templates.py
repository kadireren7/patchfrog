"""Z8 -- policy template corpus (spec test Z18)."""

from __future__ import annotations

from patchfrog.analysis.domain import FindingCategory
from patchfrog.governance.domain import PolicyScope
from patchfrog.governance.templates import PolicyTemplate, build_template_policy


def test_default_template_expresses_no_additional_opinion() -> None:
    rule = build_template_policy(PolicyTemplate.DEFAULT, scope=PolicyScope.REPOSITORY)
    assert rule.security_requires_human_review_severity_floor is None
    assert rule.verification_required_categories is None


def test_strict_security_template_lowers_human_review_floor() -> None:
    rule = build_template_policy(PolicyTemplate.STRICT_SECURITY, scope=PolicyScope.ORGANIZATION)
    assert rule.security_requires_human_review_severity_floor is not None
    assert rule.security_suppression_forbidden is True


def test_agent_heavy_template_requires_verification() -> None:
    rule = build_template_policy(PolicyTemplate.AGENT_HEAVY, scope=PolicyScope.REPOSITORY)
    assert FindingCategory.CORRECTNESS in (rule.verification_required_categories or frozenset())
    assert FindingCategory.SECURITY in (rule.verification_required_categories or frozenset())


def test_template_scope_is_always_caller_supplied() -> None:
    for template in PolicyTemplate:
        rule = build_template_policy(template, scope=PolicyScope.REPOSITORY)
        assert rule.scope is PolicyScope.REPOSITORY


def test_every_template_produces_a_real_policy_rule_never_a_hidden_object() -> None:
    from patchfrog.governance.domain import PolicyRule

    for template in PolicyTemplate:
        rule = build_template_policy(template, scope=PolicyScope.ORGANIZATION)
        assert isinstance(rule, PolicyRule)
