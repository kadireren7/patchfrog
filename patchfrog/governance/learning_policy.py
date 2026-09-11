"""Z16: governance controls learning -- never the reverse.

Two narrow, pure integration points into
:mod:`patchfrog.learning_records.personalization`'s existing functions
-- neither adds new derivation logic, both only ever *filter* what a
policy-unaware caller would otherwise see.
"""

from __future__ import annotations

from patchfrog.analysis.domain import FindingCategory
from patchfrog.governance.domain import EffectivePolicy
from patchfrog.learning_records.domain import LearningType, RepositoryLearningRecord


def personalization_effects_allowed(policy: EffectivePolicy) -> bool:
    """False means: learnings may still be retained/recomputed for audit
    (this function never touches persistence), but no personalization
    effect (:mod:`patchfrog.learning_records.personalization`) may be
    applied to a live review."""

    return policy.personalization_enabled


def filter_noise_records_for_policy(
    records: tuple[RepositoryLearningRecord, ...], *, policy: EffectivePolicy
) -> tuple[RepositoryLearningRecord, ...]:
    """Removes any ``NOISE_SUPPRESSION`` record on a ``SECURITY``-category
    surface when ``policy.security_suppression_forbidden`` is set --
    the platform default (see
    :data:`patchfrog.governance.precedence.PLATFORM_POLICY`) already
    sets this, so a SECURITY noise-suppression advisory is forbidden
    everywhere unless a future, more permissive platform policy
    deliberately changes that constant. Non-SECURITY records, and every
    ``USEFUL_FINDING_PATTERN`` record, are never filtered by this
    function regardless of policy -- suppression is what governance
    constrains, not priority boosting."""

    if not policy.security_suppression_forbidden:
        return records
    return tuple(
        r
        for r in records
        if not (r.learning_type is LearningType.NOISE_SUPPRESSION and r.surface.category is FindingCategory.SECURITY)
    )
