"""Unit tests for :mod:`patchfrog.cross_pr_intelligence.evidence` --
bounded, per-candidate evidence text, empty for every candidate with no
cross-PR overlap of its own. Never tells the LLM a conclusion."""

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
from patchfrog.cross_pr_intelligence.evidence import evidence_text_for_candidate
from patchfrog.review.domain import ReviewCandidate, ReviewCandidateReason


def _candidate(*, file_path: str, qualified_name: str | None) -> ReviewCandidate:
    return ReviewCandidate(
        file_path=file_path, symbol_id=uuid.uuid4() if qualified_name else None,
        symbol_name=qualified_name.rsplit(".", 1)[-1] if qualified_name else None,
        qualified_name=qualified_name, start_line=1, end_line=5, changed_lines=(1,),
        static_finding_ids=(), reason=ReviewCandidateReason.CHANGED_SYMBOL,
    )


def _report_with_signal(*, file_path: str, qualified_name: str) -> CrossPRIntelligenceReport:
    peer = CrossPRPeer(
        pull_request_id=uuid.uuid4(), github_pr_number=42, head_commit_sha="c1",
        review_run_id=uuid.uuid4(), sequence_number=1,
    )
    overlap = CrossPROverlap(
        peer=peer, overlap_kind=CrossPROverlapKind.SAME_CHANGED_SYMBOL,
        file_path=file_path, qualified_name=qualified_name,
    )
    signal = CrossPRSignal(
        surface_file_path=file_path, surface_qualified_name=qualified_name,
        signal_kind=CrossPROverlapKind.SAME_CHANGED_SYMBOL, supporting_overlaps=(overlap,),
        review_hint=CrossPRReviewHint.REQUIRE_CRITIC,
        evidence="this exact surface is also changed in PR #42 in this same repository",
    )
    return CrossPRIntelligenceReport(version=1, peers_considered=(peer,), overlaps=(overlap,), signals=(signal,))


def test_empty_for_candidate_with_no_signal() -> None:
    report = _report_with_signal(file_path="service.py", qualified_name="process_payment")
    unrelated = _candidate(file_path="other.py", qualified_name="other_fn")
    assert evidence_text_for_candidate(report, unrelated) == ""


def test_populated_for_the_exact_match_candidate() -> None:
    report = _report_with_signal(file_path="service.py", qualified_name="process_payment")
    candidate = _candidate(file_path="service.py", qualified_name="process_payment")
    text = evidence_text_for_candidate(report, candidate)
    assert "process_payment" in text
    assert "same_changed_symbol" in text
    assert "#42" in text
    # Never a conclusion, never blame.
    assert "bug" not in text.lower()
    assert "wrong" not in text.lower()
    assert "probably" not in text.lower()


def test_empty_for_module_region_candidate() -> None:
    report = _report_with_signal(file_path="service.py", qualified_name="process_payment")
    candidate = _candidate(file_path="service.py", qualified_name=None)
    assert evidence_text_for_candidate(report, candidate) == ""


def test_empty_report_never_crashes() -> None:
    report = CrossPRIntelligenceReport(version=1, peers_considered=(), overlaps=(), signals=())
    candidate = _candidate(file_path="service.py", qualified_name="process_payment")
    assert evidence_text_for_candidate(report, candidate) == ""
