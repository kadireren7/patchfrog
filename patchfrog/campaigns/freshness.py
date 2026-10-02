"""Data-freshness semantics for campaigns (M10.7).

A blast radius computed from dependency discovery that is old, missing or
no longer reachable must say so. Nothing here reads a clock on its own:
``now`` is always passed in, so every decision is reproducible.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from patchfrog.campaigns.domain import EnrolledRepository, Freshness, RepositoryAccess

DEFAULT_FRESHNESS_WINDOW = timedelta(days=7)
#: A discovery timestamp this far in the future is a clock/record fault,
#: not evidence of freshness.
CLOCK_SKEW_TOLERANCE = timedelta(minutes=5)


@dataclass(frozen=True, slots=True)
class FreshnessPolicy:
    window: timedelta = DEFAULT_FRESHNESS_WINDOW

    def __post_init__(self) -> None:
        if self.window <= timedelta(0):
            raise ValueError("freshness window must be positive")


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def assess_freshness(
    repository: EnrolledRepository, *, now: datetime, policy: FreshnessPolicy | None = None
) -> Freshness:
    policy = policy or FreshnessPolicy()
    if repository.access is RepositoryAccess.ACCESS_LOST:
        return Freshness.ACCESS_LOST
    if repository.last_discovery_at is None:
        return Freshness.UNKNOWN
    discovered, current = _aware(repository.last_discovery_at), _aware(now)
    if discovered - current > CLOCK_SKEW_TOLERANCE:
        return Freshness.UNKNOWN
    if current - discovered > policy.window:
        return Freshness.STALE
    return Freshness.FRESH


__all__ = ["CLOCK_SKEW_TOLERANCE", "DEFAULT_FRESHNESS_WINDOW", "FreshnessPolicy", "assess_freshness"]
