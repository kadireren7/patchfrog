"""Pure domain model for Compatibility Campaigns (M10).

One upstream API/SDK change may reach several repositories of one
workspace. A :class:`CompatibilityCampaign` is the single, durable record of
that fact: which repositories were in scope, how each was classified, and how
far each migration got -- never a pile of unrelated per-repo results.

Everything here is pure data. No I/O, no provider calls, no model.

**Honesty rules baked into the vocabulary**

* A repository whose dependency discovery is stale, missing or lost is never
  ``NOT_AFFECTED``; it is ``STALE`` / ``UNKNOWN`` / ``ACCESS_LOST``.
* ``RESOLVED`` is only ever reached by *fresh re-evaluation* showing a
  previously affected repository is no longer affected -- never by "a PR was
  opened" or "a patch exists".
* A campaign is ``RESOLVED`` only when every repository in scope is
  ``NOT_AFFECTED`` or ``RESOLVED`` (see :mod:`patchfrog.campaigns.state`).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from types import MappingProxyType

#: Bumped when campaign identity, state semantics or aggregation rules
#: change such that a previously recorded campaign is no longer what
#: re-running would produce. Part of the campaign identity key.
CAMPAIGN_ENGINE_VERSION = 1

#: Free-text fields stored on a record are bounded.
MAX_REASON_CHARS = 400
MAX_REASONS = 12
MAX_ERROR_CHARS = 300


class Freshness(StrEnum):
    """How current the dependency discovery behind a repository's evidence is."""

    FRESH = "fresh"
    STALE = "stale"
    UNKNOWN = "unknown"
    ACCESS_LOST = "access_lost"


class RepositoryAccess(StrEnum):
    """Whether PatchFrog may currently read the repository. Taken from the
    installation/enrollment record -- never assumed."""

    AVAILABLE = "available"
    ACCESS_LOST = "access_lost"


class RepoState(StrEnum):
    """Per-repository campaign state. The first block mirrors the spec's
    vocabulary; the second block exists so uncertainty is representable
    instead of being rounded to ``NOT_AFFECTED``."""

    NOT_AFFECTED = "not_affected"
    IMPACTED = "impacted"
    MIGRATION_PLANNED = "migration_planned"
    PATCH_GENERATED = "patch_generated"
    VERIFYING = "verifying"
    VERIFIED = "verified"
    PARTIALLY_VERIFIED = "partially_verified"
    HUMAN_REQUIRED = "human_required"
    FAILED = "failed"
    PR_OPENED = "pr_opened"
    RESOLVED = "resolved"
    # -- uncertainty / honesty states
    #: Discovery is older than the freshness window; requires rediscovery.
    STALE = "stale"
    #: No usable discovery, or only version-level evidence reaches the change.
    UNKNOWN = "unknown"
    #: The repository is no longer readable under the installation.
    ACCESS_LOST = "access_lost"
    #: A patch exists but verification produced insufficient evidence.
    UNVERIFIED = "unverified"


class CampaignState(StrEnum):
    DETECTED = "detected"
    ANALYZING = "analyzing"
    ACTION_REQUIRED = "action_required"
    MIGRATING = "migrating"
    PARTIALLY_RESOLVED = "partially_resolved"
    RESOLVED = "resolved"
    BLOCKED = "blocked"


class OrgClass(StrEnum):
    """Organization blast-radius classification of one repository."""

    NOT_AFFECTED = "not_affected"
    AFFECTED = "affected"
    UNKNOWN = "unknown"


class UnknownReason(StrEnum):
    STALE_DISCOVERY = "stale_discovery"
    NO_DISCOVERY = "no_discovery"
    ACCESS_LOST = "access_lost"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"


#: States from which work is still progressing without a human decision.
IN_FLIGHT_STATES: frozenset[RepoState] = frozenset(
    {
        RepoState.MIGRATION_PLANNED, RepoState.PATCH_GENERATED, RepoState.VERIFYING, RepoState.VERIFIED,
        RepoState.PR_OPENED,
    }
)
#: States that need a human or an operator action before the campaign can finish.
#: ``IMPACTED`` is blocking by design: it is only ever a final evaluation
#: state when no migration was generated (detect-only policy, or no
#: checkout), so someone must decide what happens next.
BLOCKING_STATES: frozenset[RepoState] = frozenset(
    {
        RepoState.IMPACTED, RepoState.HUMAN_REQUIRED, RepoState.FAILED, RepoState.PARTIALLY_VERIFIED, RepoState.UNVERIFIED,
        RepoState.STALE, RepoState.UNKNOWN, RepoState.ACCESS_LOST,
    }
)
#: Done: nothing more to do for this repository.
DONE_STATES: frozenset[RepoState] = frozenset({RepoState.NOT_AFFECTED, RepoState.RESOLVED})
#: Blocked by something no human decision can fix inside the campaign itself.
SYSTEM_BLOCKED_STATES: frozenset[RepoState] = frozenset({RepoState.FAILED, RepoState.ACCESS_LOST})


@dataclass(frozen=True, slots=True)
class EnrolledRepository:
    """One repository the workspace has explicitly made available.

    ``last_discovery_at`` is the last *successful* dependency discovery;
    ``None`` means there never was one. A repository that is not in the list
    of enrolled repositories is simply not in scope -- there is no implicit
    access (M10.6)."""

    full_name: str
    access: RepositoryAccess = RepositoryAccess.AVAILABLE
    last_discovery_at: datetime | None = None
    last_discovery_commit_sha: str | None = None
    installation_id: int | None = None


@dataclass(frozen=True, slots=True)
class RepositoryRecord:
    """Everything a campaign knows about one repository. All fields are
    bounded, controlled values -- never source text, never secrets."""

    repository: str
    state: RepoState
    org_class: OrgClass
    freshness: Freshness
    unknown_reason: UnknownReason | None = None
    #: M6 repository status (``affected``/``uncertain``/``unaffected``) or ``None``.
    impact_status: str | None = None
    direct_consumers: int = 0
    transitive_consumers: int = 0
    potential_consumers: int = 0
    #: Event compatibility class that reaches this repository, or ``"none"``.
    severity: str = "none"
    migration_status: str | None = None
    #: Whether any deterministic automatic migration step exists.
    migration_strategy_available: bool = False
    #: ``sandbox`` (executable verification ran) | ``static_only`` | ``not_applicable``.
    verification_feasibility: str = "not_applicable"
    verification_outcome: str | None = None
    #: ``auto_open`` | ``open_with_operator_approval`` | ``plan_only`` | ``not_eligible`` | ``None``.
    publication_readiness: str | None = None
    base_commit_sha: str | None = None
    plan_fingerprint: str | None = None
    patch_fingerprint: str | None = None
    bundle_fingerprint: str | None = None
    pr_status: str | None = None
    pr_number: int | None = None
    pr_url: str | None = None
    residual_risk: str | None = None
    #: True once any evaluation classified this repository as affected. Lets a
    #: later "no longer affected" re-evaluation become RESOLVED, not NOT_AFFECTED.
    ever_affected: bool = False
    reasons: tuple[str, ...] = ()
    #: Bounded descriptions of what a human must do (unresolved migration steps).
    human_actions: tuple[str, ...] = ()
    error: str | None = None
    evaluated_at: datetime | None = None

    @property
    def requires_rediscovery(self) -> bool:
        return self.state in (RepoState.STALE, RepoState.UNKNOWN) and self.unknown_reason in (
            UnknownReason.STALE_DISCOVERY, UnknownReason.NO_DISCOVERY,
        )


@dataclass(frozen=True, slots=True)
class OrgBlastRadius:
    """Organization-level blast radius. ``safe`` is only ever true when every
    enrolled repository is positively known to be unaffected."""

    total_enrolled: int
    potentially_relevant: int
    affected: tuple[str, ...]
    not_affected: tuple[str, ...]
    unknown: tuple[str, ...]
    unknown_reasons: Mapping[str, str] = field(default_factory=lambda: MappingProxyType({}))

    @property
    def safe(self) -> bool:
        return self.total_enrolled > 0 and not self.affected and not self.unknown

    def summary(self) -> dict[str, int]:
        return {
            "total_enrolled": self.total_enrolled,
            "potentially_relevant": self.potentially_relevant,
            "affected": len(self.affected),
            "not_affected": len(self.not_affected),
            "unknown": len(self.unknown),
        }


def campaign_identity_key(*, workspace_key: str, change_fingerprint: str, engine_version: int = CAMPAIGN_ENGINE_VERSION) -> str:
    """One campaign per workspace + upstream change + engine version (M10.9)."""

    payload = {"workspace": workspace_key, "change": change_fingerprint, "engine": engine_version}
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class CompatibilityCampaign:
    campaign_id: str
    workspace_key: str
    change_fingerprint: str
    #: Display label of the dependency/provider the change is about.
    dependency_label: str
    provider_key: str | None
    #: ``external`` (a provider/SDK) or ``internal`` (an explicit internal contract).
    origin: str
    discovered_at: datetime
    state: CampaignState
    org_blast_radius: OrgBlastRadius
    records: tuple[RepositoryRecord, ...]
    unresolved_risks: tuple[str, ...]
    #: Bumped on every recorded transition of the campaign's own state.
    version: int = 1
    engine_version: int = CAMPAIGN_ENGINE_VERSION
    #: Repository whose contract changed, for an internal-contract campaign.
    producer_repository: str | None = None
    old_version: str | None = None
    new_version: str | None = None
    compatibility: str | None = None

    @property
    def repositories_in_scope(self) -> tuple[str, ...]:
        return tuple(r.repository for r in self.records)

    @property
    def affected_repositories(self) -> tuple[str, ...]:
        return tuple(r.repository for r in self.records if r.org_class is OrgClass.AFFECTED)

    def record_for(self, repository: str) -> RepositoryRecord | None:
        return next((r for r in self.records if r.repository == repository), None)

    def state_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for record in self.records:
            counts[record.state.value] = counts.get(record.state.value, 0) + 1
        return dict(sorted(counts.items()))


def bound_reasons(reasons: tuple[str, ...] | list[str]) -> tuple[str, ...]:
    seen: list[str] = []
    for reason in reasons:
        text = reason.strip()[:MAX_REASON_CHARS]
        if text and text not in seen:
            seen.append(text)
    return tuple(seen[:MAX_REASONS])


__all__ = [
    "BLOCKING_STATES",
    "CAMPAIGN_ENGINE_VERSION",
    "DONE_STATES",
    "IN_FLIGHT_STATES",
    "MAX_ERROR_CHARS",
    "SYSTEM_BLOCKED_STATES",
    "CampaignState",
    "CompatibilityCampaign",
    "EnrolledRepository",
    "Freshness",
    "OrgBlastRadius",
    "OrgClass",
    "RepoState",
    "RepositoryAccess",
    "RepositoryRecord",
    "UnknownReason",
    "bound_reasons",
    "campaign_identity_key",
]
