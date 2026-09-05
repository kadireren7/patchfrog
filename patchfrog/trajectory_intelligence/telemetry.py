"""Compact, persistence-ready summary of a
:class:`~patchfrog.trajectory_intelligence.domain.TrajectoryIntelligenceReport`
-- counts only, mirroring Milestone O's own telemetry-shape precedent
(no rendered text at all: this package has no standalone publication
block -- spec section 21, "prefer no user-facing trajectory copy in
v1")."""

from __future__ import annotations

from dataclasses import dataclass

from patchfrog.trajectory_intelligence.domain import TrajectoryIntelligenceReport


@dataclass(frozen=True, slots=True)
class TrajectoryIntelligenceSummary:
    version: int
    trajectory_head_count: int
    trajectory_event_count: int
    trajectory_signal_count: int
    repeated_surface_churn_count: int
    trajectory_require_critic_count: int
    trajectory_deepen_context_count: int


def summarize_for_persistence(report: TrajectoryIntelligenceReport) -> TrajectoryIntelligenceSummary:
    return TrajectoryIntelligenceSummary(
        version=report.version,
        trajectory_head_count=report.head_count,
        trajectory_event_count=report.event_count,
        trajectory_signal_count=report.signal_count,
        repeated_surface_churn_count=report.repeated_surface_churn_count,
        trajectory_require_critic_count=report.require_critic_count,
        trajectory_deepen_context_count=report.deepen_context_count,
    )
