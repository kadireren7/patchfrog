"""Compatibility campaigns (M10): one upstream change, many repositories,
one coherent, honest, idempotent record."""

from __future__ import annotations

from patchfrog.campaigns.domain import (
    CAMPAIGN_ENGINE_VERSION,
    CampaignState,
    CompatibilityCampaign,
    EnrolledRepository,
    Freshness,
    OrgBlastRadius,
    OrgClass,
    RepositoryAccess,
    RepositoryRecord,
    RepoState,
    campaign_identity_key,
)

__all__ = [
    "CAMPAIGN_ENGINE_VERSION",
    "CampaignState",
    "CompatibilityCampaign",
    "EnrolledRepository",
    "Freshness",
    "OrgBlastRadius",
    "OrgClass",
    "RepoState",
    "RepositoryAccess",
    "RepositoryRecord",
    "campaign_identity_key",
]
