"""Campaign launch gate (M11.5/M11.11).

A detected change becomes a campaign only if it carries enough *evidence of
impact*. By default that means ``BREAKING`` -- structural proof (an OpenAPI
diff, an SDK-surface diff). A bare version bump of a package is
``REVIEW_REQUIRED`` at most (the evidence is a version number, not a proven
incompatibility) and is recorded as an event without opening a campaign,
unless a workspace explicitly lowers the bar. Weak signals never become
standalone work.
"""

from __future__ import annotations

from dataclasses import dataclass

from patchfrog.upstream.domain import ChangeRisk, ExternalChangeEvent

_RANK = {ChangeRisk.SAFE: 0, ChangeRisk.LOW_RISK: 1, ChangeRisk.REVIEW_REQUIRED: 2, ChangeRisk.BREAKING: 3}
DEFAULT_MIN_RISK = ChangeRisk.BREAKING


@dataclass(frozen=True, slots=True)
class LaunchDecision:
    launch: bool
    reason: str


def should_launch_campaign(event: ExternalChangeEvent, *, min_risk: ChangeRisk = DEFAULT_MIN_RISK) -> LaunchDecision:
    risk = event.classification.risk
    if _RANK[risk] >= _RANK[min_risk]:
        return LaunchDecision(True, f"change risk {risk.value} meets the campaign threshold ({min_risk.value})")
    return LaunchDecision(
        False,
        f"change risk {risk.value} is below the campaign threshold ({min_risk.value}); recorded, no campaign opened",
    )


__all__ = ["DEFAULT_MIN_RISK", "LaunchDecision", "should_launch_campaign"]
