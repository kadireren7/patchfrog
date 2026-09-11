"""Z1/Z6: explicit, provably tightening-only precedence merge.

``merge_policies(platform, organization, repository)`` is the **only**
place scope precedence is decided -- callers never compare
:class:`~patchfrog.governance.domain.PolicyRule` objects themselves.
Each field uses the single merge direction that can only ever become
*more* restrictive as more scopes weigh in (see each field's own
docstring on :class:`~patchfrog.governance.domain.PolicyRule`) -- never
"last one wins."
"""

from __future__ import annotations

from patchfrog.analysis.domain import FindingCategory, Severity
from patchfrog.governance.domain import (
    EffectivePolicy,
    PolicyRule,
    PolicyScope,
    stricter_severity_floor,
)

#: The platform safety floor -- a single, hard-coded constant, never
#: configurable by Cloud or a repository. Matches Merge Readiness's own
#: existing `_BLOCKING_SEVERITIES` floor (HIGH) exactly, so a deployment
#: with zero Cloud governance configured behaves identically to before
#: this milestone (see patchfrog.merge_readiness.service._BLOCKING_SEVERITIES).
PLATFORM_POLICY = PolicyRule(
    scope=PolicyScope.PLATFORM,
    security_block_severity_floor=Severity.HIGH,
    security_requires_human_review_severity_floor=None,
    security_suppression_forbidden=True,
    verification_required_categories=frozenset(),
    verification_required_path_prefixes=frozenset(),
    allowed_providers=None,
    personalization_enabled=True,
)


def merge_policies(
    platform: PolicyRule, organization: PolicyRule | None, repository: PolicyRule | None
) -> EffectivePolicy:
    """``platform`` should always be :data:`PLATFORM_POLICY` in
    production; accepted as a parameter (rather than hard-coded inside
    this function) purely so tests can exercise the merge logic against
    a synthetic platform floor without depending on that constant's
    exact values."""

    scopes = [platform]
    if organization is not None:
        scopes.append(organization)
    if repository is not None:
        scopes.append(repository)

    security_block_severity_floor: Severity | None = None
    security_requires_human_review_severity_floor: Severity | None = None
    for rule in scopes:
        security_block_severity_floor = stricter_severity_floor(
            security_block_severity_floor, rule.security_block_severity_floor
        )
        security_requires_human_review_severity_floor = stricter_severity_floor(
            security_requires_human_review_severity_floor, rule.security_requires_human_review_severity_floor
        )

    security_suppression_forbidden = any(rule.security_suppression_forbidden for rule in scopes if rule.security_suppression_forbidden is not None)

    verification_required_categories: frozenset[FindingCategory] = frozenset()
    verification_required_path_prefixes: frozenset[str] = frozenset()
    for rule in scopes:
        if rule.verification_required_categories:
            verification_required_categories |= rule.verification_required_categories
        if rule.verification_required_path_prefixes:
            verification_required_path_prefixes |= rule.verification_required_path_prefixes

    allowed_providers: frozenset[str] | None = None
    for rule in scopes:
        if rule.allowed_providers is None:
            continue
        allowed_providers = rule.allowed_providers if allowed_providers is None else (allowed_providers & rule.allowed_providers)

    personalization_enabled = all(
        rule.personalization_enabled for rule in scopes if rule.personalization_enabled is not None
    ) if any(rule.personalization_enabled is not None for rule in scopes) else True

    return EffectivePolicy(
        security_block_severity_floor=security_block_severity_floor,
        security_requires_human_review_severity_floor=security_requires_human_review_severity_floor,
        security_suppression_forbidden=security_suppression_forbidden,
        verification_required_categories=verification_required_categories,
        verification_required_path_prefixes=verification_required_path_prefixes,
        allowed_providers=allowed_providers,
        personalization_enabled=personalization_enabled,
        contributing_scopes=tuple(rule.scope for rule in scopes),
    )
