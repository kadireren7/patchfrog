"""Unit tests for :mod:`patchfrog.trajectory_intelligence.telemetry` --
counts only, no rendered text, no per-surface identity, no
developer/author metrics of any kind."""

from __future__ import annotations

import uuid

from patchfrog.trajectory_intelligence.domain import (
    TrajectoryEvent,
    TrajectoryEventKind,
    TrajectoryHead,
    TrajectoryIntelligenceReport,
    TrajectoryReviewHint,
    TrajectorySignal,
    TrajectorySignalKind,
)
from patchfrog.trajectory_intelligence.telemetry import summarize_for_persistence


def _head(sequence_number: int) -> TrajectoryHead:
    return TrajectoryHead(
        generation_id=uuid.uuid4(), review_run_id=uuid.uuid4(), commit_sha=f"c{sequence_number}",
        sequence_number=sequence_number, observed_at=f"2026-01-{sequence_number:02d}T00:00:00+00:00",
    )


def test_persistence_summary_empty_report() -> None:
    report = TrajectoryIntelligenceReport(version=1, lineage_valid=False, heads_considered=(), events=(), signals=())
    summary = summarize_for_persistence(report)
    assert summary.trajectory_head_count == 0
    assert summary.trajectory_event_count == 0
    assert summary.trajectory_signal_count == 0
    assert summary.repeated_surface_churn_count == 0
    assert summary.trajectory_require_critic_count == 0
    assert summary.trajectory_deepen_context_count == 0


def test_persistence_summary_counts() -> None:
    heads = tuple(_head(i) for i in range(1, 4))
    events = tuple(
        TrajectoryEvent(head=h, file_path="service.py", qualified_name="process_payment", event_kind=TrajectoryEventKind.SURFACE_CHANGED)
        for h in heads
    )
    signal = TrajectorySignal(
        surface_file_path="service.py", surface_qualified_name="process_payment",
        signal_kind=TrajectorySignalKind.REPEATED_SURFACE_CHURN, supporting_events=events,
        review_hint=TrajectoryReviewHint.REQUIRE_CRITIC, evidence="evidence",
    )
    report = TrajectoryIntelligenceReport(
        version=1, lineage_valid=True, heads_considered=heads, events=events, signals=(signal,),
    )
    summary = summarize_for_persistence(report)
    assert summary.trajectory_head_count == 3
    assert summary.trajectory_event_count == 3
    assert summary.trajectory_signal_count == 1
    assert summary.repeated_surface_churn_count == 1
    assert summary.trajectory_require_critic_count == 1
    assert summary.trajectory_deepen_context_count == 0


def test_no_text_fields_on_summary() -> None:
    from dataclasses import fields

    from patchfrog.trajectory_intelligence.telemetry import TrajectoryIntelligenceSummary

    field_names = {f.name for f in fields(TrajectoryIntelligenceSummary)}
    assert not any(name.endswith("_text") or name.endswith("_rendered") for name in field_names)
