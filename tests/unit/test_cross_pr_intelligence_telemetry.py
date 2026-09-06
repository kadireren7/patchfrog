"""Unit tests for :mod:`patchfrog.cross_pr_intelligence.telemetry` --
counts only, no rendered text, no PR number, no author, no
developer-level metrics of any kind."""

from __future__ import annotations

import uuid

from patchfrog.cross_pr_intelligence.domain import (
    CrossPRIntelligenceReport,
    CrossPROverlap,
    CrossPROverlapKind,
    CrossPRPeer,
    CrossPRReviewHint,
    CrossPRSignal,
)
from patchfrog.cross_pr_intelligence.telemetry import summarize_for_persistence


def _peer(pr_number: int) -> CrossPRPeer:
    return CrossPRPeer(
        pull_request_id=uuid.uuid4(), github_pr_number=pr_number, head_commit_sha="c1",
        review_run_id=uuid.uuid4(), sequence_number=1,
    )


def test_persistence_summary_empty_report() -> None:
    report = CrossPRIntelligenceReport(version=1, peers_considered=(), overlaps=(), signals=())
    summary = summarize_for_persistence(report)
    assert summary.cross_pr_peer_count == 0
    assert summary.cross_pr_overlap_count == 0
    assert summary.cross_pr_signal_count == 0
    assert summary.cross_pr_same_changed_symbol_count == 0
    assert summary.cross_pr_require_critic_count == 0
    assert summary.cross_pr_deepen_context_count == 0


def test_persistence_summary_counts() -> None:
    peer = _peer(42)
    overlap = CrossPROverlap(
        peer=peer, overlap_kind=CrossPROverlapKind.SAME_CHANGED_SYMBOL,
        file_path="service.py", qualified_name="process_payment",
    )
    signal = CrossPRSignal(
        surface_file_path="service.py", surface_qualified_name="process_payment",
        signal_kind=CrossPROverlapKind.SAME_CHANGED_SYMBOL, supporting_overlaps=(overlap,),
        review_hint=CrossPRReviewHint.REQUIRE_CRITIC, evidence="evidence",
    )
    report = CrossPRIntelligenceReport(
        version=1, peers_considered=(peer,), overlaps=(overlap,), signals=(signal,),
    )
    summary = summarize_for_persistence(report)
    assert summary.cross_pr_peer_count == 1
    assert summary.cross_pr_overlap_count == 1
    assert summary.cross_pr_signal_count == 1
    assert summary.cross_pr_same_changed_symbol_count == 1
    assert summary.cross_pr_require_critic_count == 1
    assert summary.cross_pr_deepen_context_count == 0


def test_no_identity_or_text_fields_on_summary() -> None:
    from dataclasses import fields

    from patchfrog.cross_pr_intelligence.telemetry import CrossPRIntelligenceSummary

    field_names = {f.name for f in fields(CrossPRIntelligenceSummary)}
    assert not any(name.endswith("_text") or name.endswith("_rendered") for name in field_names)
    assert not any("pr_number" in name or "author" in name or "title" in name for name in field_names)
