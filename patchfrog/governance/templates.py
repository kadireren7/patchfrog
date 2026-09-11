"""Z8: minimal policy templates -- starting points a Cloud UI can offer,
never hidden semantics of their own. Each is a thin constructor over the
exact same :class:`~patchfrog.governance.domain.PolicyRule` shape every
hand-authored policy uses; nothing here is evaluated any differently
than an operator-authored rule at the same scope."""

from __future__ import annotations

from enum import StrEnum

from patchfrog.analysis.domain import FindingCategory, Severity
from patchfrog.governance.domain import PolicyRule, PolicyScope


class PolicyTemplate(StrEnum):
    DEFAULT = "default"
    STRICT_SECURITY = "strict_security"
    AGENT_HEAVY = "agent_heavy"


def build_template_policy(template: PolicyTemplate, *, scope: PolicyScope) -> PolicyRule:
    """``scope`` is always supplied by the caller (Cloud knows whether
    this template is being applied at organization or repository scope)
    -- this function never guesses it."""

    if template is PolicyTemplate.DEFAULT:
        # Expresses no additional opinion beyond the platform floor --
        # every field left None.
        return PolicyRule(scope=scope)

    if template is PolicyTemplate.STRICT_SECURITY:
        return PolicyRule(
            scope=scope,
            security_requires_human_review_severity_floor=Severity.MEDIUM,
            security_suppression_forbidden=True,
        )

    if template is PolicyTemplate.AGENT_HEAVY:
        # Z9: for repositories with a lot of AI-generated code -- always
        # explicitly opted into by an operator/workspace owner, never
        # auto-detected from code style (Z9's own explicit instruction).
        return PolicyRule(
            scope=scope,
            verification_required_categories=frozenset({FindingCategory.CORRECTNESS, FindingCategory.SECURITY}),
            security_requires_human_review_severity_floor=Severity.MEDIUM,
        )

    raise ValueError(f"unknown policy template: {template!r}")  # pragma: no cover -- exhaustive above
