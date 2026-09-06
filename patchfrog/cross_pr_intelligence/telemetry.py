"""Compact, persistence-ready summary of a
:class:`~patchfrog.cross_pr_intelligence.domain.CrossPRIntelligenceReport`
-- counts only, mirroring Trajectory Intelligence's own telemetry-shape
precedent (no rendered text, no PR number, no author, no title
anywhere -- this package has no standalone publication block)."""

from __future__ import annotations

from dataclasses import dataclass

from patchfrog.cross_pr_intelligence.domain import CrossPRIntelligenceReport


@dataclass(frozen=True, slots=True)
class CrossPRIntelligenceSummary:
    version: int
    cross_pr_peer_count: int
    cross_pr_overlap_count: int
    cross_pr_signal_count: int
    cross_pr_same_changed_symbol_count: int
    cross_pr_require_critic_count: int
    cross_pr_deepen_context_count: int


def summarize_for_persistence(report: CrossPRIntelligenceReport) -> CrossPRIntelligenceSummary:
    return CrossPRIntelligenceSummary(
        version=report.version,
        cross_pr_peer_count=report.peer_count,
        cross_pr_overlap_count=report.overlap_count,
        cross_pr_signal_count=report.signal_count,
        cross_pr_same_changed_symbol_count=report.same_changed_symbol_count,
        cross_pr_require_critic_count=report.require_critic_count,
        cross_pr_deepen_context_count=report.deepen_context_count,
    )
