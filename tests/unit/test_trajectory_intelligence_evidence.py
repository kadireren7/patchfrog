"""Unit tests for :mod:`patchfrog.trajectory_intelligence.evidence` --
bounded, per-candidate evidence text, empty for every candidate with no
trajectory signal of its own. Never tells the LLM a conclusion."""

from __future__ import annotations

import uuid

from patchfrog.review.domain import ReviewCandidate, ReviewCandidateReason
from patchfrog.trajectory_intelligence.domain import (
    TrajectoryEvent,
    TrajectoryEventKind,
    TrajectoryHead,
    TrajectoryIntelligenceReport,
    TrajectoryReviewHint,
    TrajectorySignal,
    TrajectorySignalKind,
)
from patchfrog.trajectory_intelligence.evidence import evidence_text_for_candidate


def _candidate(*, file_path: str, qualified_name: str | None) -> ReviewCandidate:
    return ReviewCandidate(
        file_path=file_path, symbol_id=uuid.uuid4() if qualified_name else None,
        symbol_name=qualified_name.rsplit(".", 1)[-1] if qualified_name else None,
        qualified_name=qualified_name, start_line=1, end_line=5, changed_lines=(1,),
        static_finding_ids=(), reason=ReviewCandidateReason.CHANGED_SYMBOL,
    )


def _head(sequence_number: int) -> TrajectoryHead:
    return TrajectoryHead(
        generation_id=uuid.uuid4(), review_run_id=uuid.uuid4(), commit_sha=f"c{sequence_number}",
        sequence_number=sequence_number, observed_at=f"2026-01-{sequence_number:02d}T00:00:00+00:00",
    )


def _report_with_signal(*, file_path: str, qualified_name: str) -> TrajectoryIntelligenceReport:
    heads = tuple(_head(i) for i in range(1, 4))
    events = tuple(
        TrajectoryEvent(head=h, file_path=file_path, qualified_name=qualified_name, event_kind=TrajectoryEventKind.SURFACE_CHANGED)
        for h in heads
    )
    signal = TrajectorySignal(
        surface_file_path=file_path, surface_qualified_name=qualified_name,
        signal_kind=TrajectorySignalKind.REPEATED_SURFACE_CHURN, supporting_events=events,
        review_hint=TrajectoryReviewHint.REQUIRE_CRITIC,
        evidence="this exact surface changed across 3 distinct heads in the current PR's own lineage",
    )
    return TrajectoryIntelligenceReport(
        version=1, lineage_valid=True, heads_considered=heads, events=events, signals=(signal,),
    )


def test_empty_for_candidate_with_no_signal() -> None:
    report = _report_with_signal(file_path="service.py", qualified_name="process_payment")
    unrelated = _candidate(file_path="other.py", qualified_name="other_fn")
    assert evidence_text_for_candidate(report, unrelated) == ""


def test_populated_for_the_exact_match_candidate() -> None:
    report = _report_with_signal(file_path="service.py", qualified_name="process_payment")
    candidate = _candidate(file_path="service.py", qualified_name="process_payment")
    text = evidence_text_for_candidate(report, candidate)
    assert "process_payment" in text
    assert "repeated_surface_churn" in text
    assert "3" in text
    # Never a conclusion.
    assert "buggy" not in text.lower()
    assert "probably" not in text.lower()


def test_empty_for_module_region_candidate() -> None:
    report = _report_with_signal(file_path="service.py", qualified_name="process_payment")
    candidate = _candidate(file_path="service.py", qualified_name=None)
    assert evidence_text_for_candidate(report, candidate) == ""


def test_empty_report_never_crashes() -> None:
    report = TrajectoryIntelligenceReport(version=1, lineage_valid=False, heads_considered=(), events=(), signals=())
    candidate = _candidate(file_path="service.py", qualified_name="process_payment")
    assert evidence_text_for_candidate(report, candidate) == ""
