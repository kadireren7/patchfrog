"""Unit tests for :mod:`patchfrog.trajectory_intelligence.matching` --
deterministic, pure derivation. Every case is a hand-built
:class:`~patchfrog.trajectory_intelligence.domain.TrajectoryHead`, no
database, no LLM -- mirrors every other Intelligence package's own
matching-unit-test discipline."""

from __future__ import annotations

import uuid

from patchfrog.trajectory_intelligence.domain import (
    MIN_SURFACE_CHURN_EVENTS,
    TrajectoryEventKind,
    TrajectoryHead,
    TrajectoryReviewHint,
    TrajectorySignalKind,
)
from patchfrog.trajectory_intelligence.matching import (
    derive_trajectory_events,
    derive_trajectory_signals,
    select_review_hint,
)

_REPO_ID = uuid.uuid4()


def _head(*, sequence_number: int, commit_sha: str | None = None, current: bool = False) -> TrajectoryHead:
    return TrajectoryHead(
        generation_id=None if current else uuid.uuid4(),
        review_run_id=None if current else uuid.uuid4(),
        commit_sha=commit_sha or f"commit-{sequence_number}",
        sequence_number=sequence_number,
        observed_at=f"2026-01-{sequence_number:02d}T00:00:00+00:00",
    )


def _rid(head: TrajectoryHead) -> uuid.UUID:
    assert head.review_run_id is not None
    return head.review_run_id


def test_no_events_no_signals() -> None:
    events = derive_trajectory_events(
        historical_heads=(), historical_surfaces_by_review_run={}, current_head=_head(sequence_number=1, current=True),
        current_changed_surfaces=(),
    )
    assert events == ()
    assert derive_trajectory_signals(events) == ()


def test_single_head_single_symbol_no_signal() -> None:
    head1 = _head(sequence_number=1)
    events = derive_trajectory_events(
        historical_heads=(head1,),
        historical_surfaces_by_review_run={_rid(head1): (("service.py", "process_payment"),)},
        current_head=_head(sequence_number=2, current=True), current_changed_surfaces=(),
    )
    assert len(events) == 1
    assert events[0].event_kind is TrajectoryEventKind.SURFACE_CHANGED
    assert derive_trajectory_signals(events) == ()


def test_two_distinct_heads_below_threshold_no_signal() -> None:
    """MIN_SURFACE_CHURN_EVENTS is 3 -- exactly 2 distinct heads must not
    trigger the signal."""

    head1, head2 = _head(sequence_number=1), _head(sequence_number=2)
    surfaces: dict[uuid.UUID, tuple[tuple[str, str], ...]] = {
        _rid(head1): (("service.py", "process_payment"),),
        _rid(head2): (("service.py", "process_payment"),),
    }
    events = derive_trajectory_events(
        historical_heads=(head1, head2), historical_surfaces_by_review_run=surfaces,
        current_head=_head(sequence_number=3, current=True), current_changed_surfaces=(),
    )
    signals = derive_trajectory_signals(events)
    assert signals == ()


def test_three_distinct_heads_at_threshold_triggers_churn() -> None:
    assert MIN_SURFACE_CHURN_EVENTS == 3
    head1, head2 = _head(sequence_number=1), _head(sequence_number=2)
    surfaces: dict[uuid.UUID, tuple[tuple[str, str], ...]] = {
        _rid(head1): (("service.py", "process_payment"),),
        _rid(head2): (("service.py", "process_payment"),),
    }
    current_head = _head(sequence_number=3, current=True)
    events = derive_trajectory_events(
        historical_heads=(head1, head2), historical_surfaces_by_review_run=surfaces,
        current_head=current_head, current_changed_surfaces=(("service.py", "process_payment"),),
    )
    signals = derive_trajectory_signals(events)
    assert len(signals) == 1
    signal = signals[0]
    assert signal.signal_kind is TrajectorySignalKind.REPEATED_SURFACE_CHURN
    assert signal.distinct_head_count == 3
    assert signal.review_hint is TrajectoryReviewHint.REQUIRE_CRITIC


def test_unrelated_symbols_never_combine() -> None:
    head1, head2, head3 = _head(sequence_number=1), _head(sequence_number=2), _head(sequence_number=3)
    surfaces: dict[uuid.UUID, tuple[tuple[str, str], ...]] = {
        _rid(head1): (("a.py", "foo"),),
        _rid(head2): (("b.py", "bar"),),
        _rid(head3): (("c.py", "baz"),),
    }
    events = derive_trajectory_events(
        historical_heads=(head1, head2, head3), historical_surfaces_by_review_run=surfaces,
        current_head=_head(sequence_number=4, current=True), current_changed_surfaces=(),
    )
    assert derive_trajectory_signals(events) == ()


def test_same_file_different_symbols_never_conflated() -> None:
    head1, head2, head3 = _head(sequence_number=1), _head(sequence_number=2), _head(sequence_number=3)
    surfaces: dict[uuid.UUID, tuple[tuple[str, str], ...]] = {
        _rid(head1): (("service.py", "foo"),),
        _rid(head2): (("service.py", "bar"),),
        _rid(head3): (("service.py", "baz"),),
    }
    events = derive_trajectory_events(
        historical_heads=(head1, head2, head3), historical_surfaces_by_review_run=surfaces,
        current_head=_head(sequence_number=4, current=True), current_changed_surfaces=(),
    )
    assert derive_trajectory_signals(events) == ()


def test_docs_only_trajectory_never_escalates() -> None:
    """A doc file is neither production nor test but still classified
    SURFACE_CHANGED -- churn detection is symbol-identity based, not
    content-type based; this proves no *escalation* happens without a
    real repeated-surface pattern."""

    head1 = _head(sequence_number=1)
    events = derive_trajectory_events(
        historical_heads=(head1,), historical_surfaces_by_review_run={_rid(head1): (("README.md", "intro"),)},
        current_head=_head(sequence_number=2, current=True), current_changed_surfaces=(),
    )
    assert derive_trajectory_signals(events) == ()


def test_test_file_classified_as_test_surface_changed() -> None:
    head1 = _head(sequence_number=1)
    events = derive_trajectory_events(
        historical_heads=(head1,),
        historical_surfaces_by_review_run={_rid(head1): (("test_service.py", "test_process_payment"),)},
        current_head=_head(sequence_number=2, current=True), current_changed_surfaces=(),
    )
    assert events[0].event_kind is TrajectoryEventKind.TEST_SURFACE_CHANGED


def test_select_review_hint_none_for_unrelated_candidate() -> None:
    hint = select_review_hint((), file_path="service.py", qualified_name="process_payment")
    assert hint is TrajectoryReviewHint.NONE


def test_select_review_hint_none_for_module_region_candidate() -> None:
    head1, head2 = _head(sequence_number=1), _head(sequence_number=2)
    surfaces: dict[uuid.UUID, tuple[tuple[str, str], ...]] = {
        _rid(head1): (("service.py", "process_payment"),),
        _rid(head2): (("service.py", "process_payment"),),
    }
    events = derive_trajectory_events(
        historical_heads=(head1, head2), historical_surfaces_by_review_run=surfaces,
        current_head=_head(sequence_number=3, current=True), current_changed_surfaces=(("service.py", "process_payment"),),
    )
    signals = derive_trajectory_signals(events)
    assert select_review_hint(signals, file_path="service.py", qualified_name=None) is TrajectoryReviewHint.NONE


def test_max_events_per_surface_bounded() -> None:
    from patchfrog.trajectory_intelligence.domain import MAX_EVENTS_PER_SURFACE

    heads = tuple(_head(sequence_number=i) for i in range(1, MAX_EVENTS_PER_SURFACE + 4))
    surfaces: dict[uuid.UUID, tuple[tuple[str, str], ...]] = {
        _rid(h): (("service.py", "process_payment"),) for h in heads
    }
    events = derive_trajectory_events(
        historical_heads=heads, historical_surfaces_by_review_run=surfaces,
        current_head=_head(sequence_number=99, current=True), current_changed_surfaces=(),
    )
    assert len(events) == MAX_EVENTS_PER_SURFACE


def test_max_trajectory_events_bounded_across_surfaces() -> None:
    from patchfrog.trajectory_intelligence.domain import MAX_TRAJECTORY_EVENTS

    head1 = _head(sequence_number=1)
    many_surfaces = tuple((f"file_{i}.py", f"symbol_{i}") for i in range(MAX_TRAJECTORY_EVENTS + 10))
    events = derive_trajectory_events(
        historical_heads=(head1,), historical_surfaces_by_review_run={_rid(head1): many_surfaces},
        current_head=_head(sequence_number=2, current=True), current_changed_surfaces=(),
    )
    assert len(events) == MAX_TRAJECTORY_EVENTS


def test_repeated_review_of_same_head_never_double_counts() -> None:
    """Two historical heads sharing the same commit_sha (a legitimate
    same-SHA retry that produced two review_run_ids) must not inflate
    the distinct-head count -- this is enforced at the query layer
    (fetch_trajectory_heads dedups by commit_sha), so at the matching
    layer this is proven by never receiving duplicate commit_shas in
    historical_heads in the first place; here we prove that even if
    duplicate SURFACE_CHANGED events for the exact same commit_sha
    somehow reached this layer, distinct_head_count still counts
    distinct SHAs, not raw event count."""

    shared_sha = "shared-commit"
    head_a = _head(sequence_number=1, commit_sha=shared_sha)
    head_b = _head(sequence_number=2)
    surfaces = {
        _rid(head_a): (("service.py", "process_payment"), ("service.py", "process_payment")),
        _rid(head_b): (("service.py", "process_payment"),),
    }
    events = derive_trajectory_events(
        historical_heads=(head_a, head_b), historical_surfaces_by_review_run=surfaces,
        current_head=_head(sequence_number=3, current=True), current_changed_surfaces=(("service.py", "process_payment"),),
    )
    signals = derive_trajectory_signals(events)
    # Only 3 distinct commit_shas (head_a, head_b, current) even though
    # head_a contributed a duplicate surface entry.
    assert len(signals) == 1
    assert signals[0].distinct_head_count == 3
