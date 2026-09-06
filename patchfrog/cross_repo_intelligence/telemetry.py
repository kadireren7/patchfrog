"""Compact, persistence-ready summary of a
:class:`~patchfrog.cross_repo_intelligence.domain.CrossRepoIntelligenceReport`
-- counts only. No repository full_name, no repository id, no contract
key string, no organization name anywhere -- this package has no
standalone publication block."""

from __future__ import annotations

from dataclasses import dataclass

from patchfrog.cross_repo_intelligence.domain import CrossRepoIntelligenceReport


@dataclass(frozen=True, slots=True)
class CrossRepoIntelligenceSummary:
    version: int
    cross_repo_peer_count: int
    cross_repo_signal_count: int
    cross_repo_explicit_contract_relation_count: int
    cross_repo_require_critic_count: int
    cross_repo_deepen_context_count: int


def summarize_for_persistence(report: CrossRepoIntelligenceReport) -> CrossRepoIntelligenceSummary:
    return CrossRepoIntelligenceSummary(
        version=report.version,
        cross_repo_peer_count=report.peer_count,
        cross_repo_signal_count=report.signal_count,
        cross_repo_explicit_contract_relation_count=report.contract_change_count,
        cross_repo_require_critic_count=report.require_critic_count,
        cross_repo_deepen_context_count=report.deepen_context_count,
    )
