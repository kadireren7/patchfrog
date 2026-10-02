"""Deterministic state semantics for repositories and campaigns (M10.3).

Two pure functions, no I/O:

* :func:`derive_repo_state` -- one repository's evaluation facts -> a
  :class:`RepoState`;
* :func:`derive_campaign_state` -- all repository states -> a
  :class:`CampaignState`.

**Campaign precedence (first match wins)**

1. no repository evaluated yet                         -> ``DETECTED``
2. some repositories still unevaluated                 -> ``ANALYZING``
3. every repo ``NOT_AFFECTED``/``RESOLVED``            -> ``RESOLVED``
4. at least one ``RESOLVED`` and anything unfinished   -> ``PARTIALLY_RESOLVED``
5. blocking repos exist, all system-blocked, none in
   flight                                              -> ``BLOCKED``
6. blocking repos exist                                -> ``ACTION_REQUIRED``
7. otherwise (only in-flight work)                     -> ``MIGRATING``

Rule 3 is the only road to ``RESOLVED``: a stale, unknown, access-lost,
failed, unverified or human-required repository can never be rounded away.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from patchfrog.campaigns.domain import (
    BLOCKING_STATES,
    DONE_STATES,
    IN_FLIGHT_STATES,
    SYSTEM_BLOCKED_STATES,
    CampaignState,
    Freshness,
    OrgClass,
    RepoState,
)


@dataclass(frozen=True, slots=True)
class RepoFacts:
    """Inputs to :func:`derive_repo_state`. All strings are the ``.value`` of
    the corresponding M6/M7/M8/M9 enum, so this module imports none of them."""

    freshness: Freshness
    org_class: OrgClass
    ever_affected: bool = False
    #: M7 ``MigrationStatus.value`` or ``None`` if no plan was made.
    migration_status: str | None = None
    has_automatic_steps: bool = False
    patch_generated: bool = False
    #: M8 ``VerificationOutcome.value`` or ``None`` if verification did not run.
    verification_outcome: str | None = None
    #: M9 ``MigrationPRStatus.value`` or ``None``.
    pr_status: str | None = None
    pr_number: int | None = None
    #: The evaluation raised; the repository is isolated, never guessed.
    failed: bool = False


_PR_OPEN_STATUSES = frozenset({"opened", "updated"})
#: M9 reports an already-existing, human-closed PR as ``no_op_unchanged`` and does not reopen it.
_PR_CLOSED_BY_HUMAN = "no_op_unchanged"


def derive_repo_state(facts: RepoFacts) -> RepoState:
    if facts.failed:
        return RepoState.FAILED
    if facts.freshness is Freshness.ACCESS_LOST:
        return RepoState.ACCESS_LOST
    if facts.freshness is Freshness.STALE:
        return RepoState.STALE
    if facts.freshness is Freshness.UNKNOWN:
        return RepoState.UNKNOWN

    if facts.org_class is OrgClass.NOT_AFFECTED:
        return RepoState.RESOLVED if facts.ever_affected else RepoState.NOT_AFFECTED
    if facts.org_class is OrgClass.UNKNOWN:
        return RepoState.UNKNOWN

    # -- affected and evidence is fresh
    outcome = facts.verification_outcome
    if outcome in ("regression_detected", "failed"):
        return RepoState.FAILED
    if outcome == "human_required":
        return RepoState.HUMAN_REQUIRED
    if facts.pr_status == _PR_CLOSED_BY_HUMAN and facts.pr_number is not None:
        # M9 never reopens a PR a human closed: the next step is a human decision.
        return RepoState.HUMAN_REQUIRED
    if facts.pr_status in _PR_OPEN_STATUSES and facts.pr_number is not None and outcome in ("verified", "partially_verified"):
        return RepoState.PR_OPENED
    if outcome == "verified":
        return RepoState.VERIFIED
    if outcome == "partially_verified":
        return RepoState.PARTIALLY_VERIFIED
    if outcome == "unverified":
        return RepoState.UNVERIFIED

    # -- no verification outcome yet
    if facts.migration_status in ("human_required", "unsupported"):
        return RepoState.HUMAN_REQUIRED
    if facts.migration_status == "failed":
        return RepoState.FAILED
    if facts.patch_generated:
        return RepoState.PATCH_GENERATED
    if facts.migration_status in ("planned", "patch_generated", "partial") and facts.has_automatic_steps:
        return RepoState.MIGRATION_PLANNED
    if facts.migration_status in ("planned", "patch_generated", "partial"):
        return RepoState.HUMAN_REQUIRED
    return RepoState.IMPACTED


def derive_campaign_state(states: Iterable[RepoState | None]) -> CampaignState:
    """``None`` marks a repository in scope that has not been evaluated yet."""

    items = list(states)
    if not items or all(s is None for s in items):
        return CampaignState.DETECTED
    if any(s is None for s in items):
        return CampaignState.ANALYZING
    evaluated: list[RepoState] = [s for s in items if s is not None]
    if all(s in DONE_STATES for s in evaluated):
        return CampaignState.RESOLVED
    if any(s is RepoState.RESOLVED for s in evaluated):
        return CampaignState.PARTIALLY_RESOLVED
    blocking = [s for s in evaluated if s in BLOCKING_STATES]
    in_flight = [s for s in evaluated if s in IN_FLIGHT_STATES]
    if blocking and not in_flight and all(s in SYSTEM_BLOCKED_STATES for s in blocking):
        return CampaignState.BLOCKED
    if blocking:
        return CampaignState.ACTION_REQUIRED
    return CampaignState.MIGRATING


__all__ = ["RepoFacts", "derive_campaign_state", "derive_repo_state"]
