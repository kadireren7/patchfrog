"""Unit tests for :mod:`patchfrog.cross_repo_intelligence.telemetry` --
counts only, no rendered text, no repository name/id, no contract key
string, no organization name of any kind."""

from __future__ import annotations

import uuid

from patchfrog.cross_repo_intelligence.domain import (
    CrossRepoIntelligenceReport,
    CrossRepoOverlap,
    CrossRepoPeer,
    CrossRepoReviewHint,
    CrossRepoSignal,
    CrossRepoSignalKind,
    RepositoryRelationKind,
)
from patchfrog.cross_repo_intelligence.telemetry import summarize_for_persistence


def _peer() -> CrossRepoPeer:
    return CrossRepoPeer(
        repository_id=uuid.uuid4(), full_name="org/service-b",
        relation_kind=RepositoryRelationKind.EXPLICIT_SHARED_CONTRACT, contract_key="payments.capture:v1",
    )


def test_persistence_summary_empty_report() -> None:
    report = CrossRepoIntelligenceReport(version=1, peers_considered=(), overlaps=(), signals=())
    summary = summarize_for_persistence(report)
    assert summary.cross_repo_peer_count == 0
    assert summary.cross_repo_signal_count == 0
    assert summary.cross_repo_explicit_contract_relation_count == 0
    assert summary.cross_repo_require_critic_count == 0
    assert summary.cross_repo_deepen_context_count == 0


def test_persistence_summary_counts() -> None:
    peer = _peer()
    overlap = CrossRepoOverlap(
        peer=peer, signal_kind=CrossRepoSignalKind.CROSS_REPO_CONTRACT_CHANGE,
        file_path="capture.py", qualified_name="capture_payment",
    )
    signal = CrossRepoSignal(
        surface_file_path="capture.py", surface_qualified_name="capture_payment",
        signal_kind=CrossRepoSignalKind.CROSS_REPO_CONTRACT_CHANGE, supporting_overlaps=(overlap,),
        review_hint=CrossRepoReviewHint.REQUIRE_CRITIC, evidence="evidence",
    )
    report = CrossRepoIntelligenceReport(
        version=1, peers_considered=(peer,), overlaps=(overlap,), signals=(signal,),
    )
    summary = summarize_for_persistence(report)
    assert summary.cross_repo_peer_count == 1
    assert summary.cross_repo_signal_count == 1
    assert summary.cross_repo_explicit_contract_relation_count == 1
    assert summary.cross_repo_require_critic_count == 1
    assert summary.cross_repo_deepen_context_count == 0


def test_no_identity_or_text_fields_on_summary() -> None:
    from dataclasses import fields

    from patchfrog.cross_repo_intelligence.telemetry import CrossRepoIntelligenceSummary

    field_names = {f.name for f in fields(CrossRepoIntelligenceSummary)}
    assert not any(name.endswith("_text") or name.endswith("_rendered") for name in field_names)
    assert not any(
        "full_name" in name or "repository_id" in name or "contract_key" in name or "organization" in name
        for name in field_names
    )
