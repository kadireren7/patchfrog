"""Unit tests for :mod:`patchfrog.cross_repo_intelligence.evidence` --
bounded, per-candidate evidence text, empty for every candidate with no
cross-repo overlap of its own. Never tells the LLM a conclusion."""

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
from patchfrog.cross_repo_intelligence.evidence import evidence_text_for_candidate
from patchfrog.review.domain import ReviewCandidate, ReviewCandidateReason


def _candidate(*, file_path: str, qualified_name: str | None) -> ReviewCandidate:
    return ReviewCandidate(
        file_path=file_path, symbol_id=uuid.uuid4() if qualified_name else None,
        symbol_name=qualified_name.rsplit(".", 1)[-1] if qualified_name else None,
        qualified_name=qualified_name, start_line=1, end_line=5, changed_lines=(1,),
        static_finding_ids=(), reason=ReviewCandidateReason.CHANGED_SYMBOL,
    )


def _report_with_signal(*, file_path: str, qualified_name: str) -> CrossRepoIntelligenceReport:
    peer = CrossRepoPeer(
        repository_id=uuid.uuid4(), full_name="org/service-b",
        relation_kind=RepositoryRelationKind.EXPLICIT_SHARED_CONTRACT, contract_key="payments.capture:v1",
    )
    overlap = CrossRepoOverlap(
        peer=peer, signal_kind=CrossRepoSignalKind.CROSS_REPO_CONTRACT_CHANGE,
        file_path=file_path, qualified_name=qualified_name,
    )
    signal = CrossRepoSignal(
        surface_file_path=file_path, surface_qualified_name=qualified_name,
        signal_kind=CrossRepoSignalKind.CROSS_REPO_CONTRACT_CHANGE, supporting_overlaps=(overlap,),
        review_hint=CrossRepoReviewHint.REQUIRE_CRITIC,
        evidence="this exact contract (payments.capture:v1) is explicitly registered as consumed by "
        "repository org/service-b",
    )
    return CrossRepoIntelligenceReport(version=1, peers_considered=(peer,), overlaps=(overlap,), signals=(signal,))


def test_empty_for_candidate_with_no_signal() -> None:
    report = _report_with_signal(file_path="capture.py", qualified_name="capture_payment")
    unrelated = _candidate(file_path="other.py", qualified_name="other_fn")
    assert evidence_text_for_candidate(report, unrelated) == ""


def test_populated_for_the_exact_match_candidate() -> None:
    report = _report_with_signal(file_path="capture.py", qualified_name="capture_payment")
    candidate = _candidate(file_path="capture.py", qualified_name="capture_payment")
    text = evidence_text_for_candidate(report, candidate)
    assert "payments.capture:v1" in text
    assert "cross_repo_contract_change" in text
    assert "org/service-b" in text
    # Never a conclusion, never blame.
    assert "will break" not in text.lower()
    assert "wrong" not in text.lower()
    assert "probably" not in text.lower()


def test_empty_for_module_region_candidate() -> None:
    report = _report_with_signal(file_path="capture.py", qualified_name="capture_payment")
    candidate = _candidate(file_path="capture.py", qualified_name=None)
    assert evidence_text_for_candidate(report, candidate) == ""


def test_empty_report_never_crashes() -> None:
    report = CrossRepoIntelligenceReport(version=1, peers_considered=(), overlaps=(), signals=())
    candidate = _candidate(file_path="capture.py", qualified_name="capture_payment")
    assert evidence_text_for_candidate(report, candidate) == ""
