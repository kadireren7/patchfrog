"""Organization blast radius (M10.2).

Turns per-repository M6 impact + freshness into one organization-level
classification: affected / not affected / unknown. The decisive rule is
that **uncertainty is never rounded to "safe"**: a repository with stale,
missing or lost discovery, or with only version-level evidence, is
``UNKNOWN`` and keeps :attr:`OrgBlastRadius.safe` false.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from patchfrog.campaigns.domain import (
    Freshness,
    OrgBlastRadius,
    OrgClass,
    UnknownReason,
)
from patchfrog.upstream.workspace import RepositoryImpact, RepositoryImpactStatus


@dataclass(frozen=True, slots=True)
class RepositoryClassification:
    repository: str
    org_class: OrgClass
    unknown_reason: UnknownReason | None
    reason: str
    #: A matching dependency exists in the repository (used or not reached).
    has_matching_dependency: bool


_UNKNOWN_BY_FRESHNESS = {
    Freshness.ACCESS_LOST: (UnknownReason.ACCESS_LOST, "repository access was lost; cannot claim it is unaffected"),
    Freshness.STALE: (
        UnknownReason.STALE_DISCOVERY, "dependency discovery is older than the freshness window; rediscovery required",
    ),
    Freshness.UNKNOWN: (UnknownReason.NO_DISCOVERY, "no successful dependency discovery exists for this repository"),
}


def classify_repository(
    repository: str, *, freshness: Freshness, impact: RepositoryImpact | None
) -> RepositoryClassification:
    if freshness in _UNKNOWN_BY_FRESHNESS:
        reason_code, reason = _UNKNOWN_BY_FRESHNESS[freshness]
        return RepositoryClassification(repository, OrgClass.UNKNOWN, reason_code, reason, False)
    if impact is None:
        return RepositoryClassification(
            repository, OrgClass.UNKNOWN, UnknownReason.NO_DISCOVERY, "no dependency inventory was available", False
        )
    matching = bool(impact.matches)
    if impact.status is RepositoryImpactStatus.AFFECTED:
        return RepositoryClassification(repository, OrgClass.AFFECTED, None, impact.reason, matching)
    if impact.status is RepositoryImpactStatus.UNCERTAIN:
        return RepositoryClassification(
            repository, OrgClass.UNKNOWN, UnknownReason.INSUFFICIENT_EVIDENCE, impact.reason, matching
        )
    return RepositoryClassification(repository, OrgClass.NOT_AFFECTED, None, impact.reason, matching)


def build_org_blast_radius(classifications: Sequence[RepositoryClassification]) -> OrgBlastRadius:
    ordered = sorted(classifications, key=lambda c: c.repository)
    affected = tuple(c.repository for c in ordered if c.org_class is OrgClass.AFFECTED)
    unknown = tuple(c.repository for c in ordered if c.org_class is OrgClass.UNKNOWN)
    not_affected = tuple(c.repository for c in ordered if c.org_class is OrgClass.NOT_AFFECTED)
    relevant = {c.repository for c in ordered if c.org_class is not OrgClass.NOT_AFFECTED or c.has_matching_dependency}
    return OrgBlastRadius(
        total_enrolled=len(ordered),
        potentially_relevant=len(relevant),
        affected=affected,
        not_affected=not_affected,
        unknown=unknown,
        unknown_reasons={c.repository: c.unknown_reason.value for c in ordered if c.unknown_reason is not None},
    )


__all__ = ["RepositoryClassification", "build_org_blast_radius", "classify_repository"]
