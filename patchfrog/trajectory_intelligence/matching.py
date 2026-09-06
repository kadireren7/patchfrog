"""Deterministic derivation of
:class:`~patchfrog.trajectory_intelligence.domain.TrajectoryEvent`\\ s
and :class:`~patchfrog.trajectory_intelligence.domain.TrajectorySignal`\\ s
-- pure, synchronous, consuming only already-fetched historical
surfaces (:mod:`patchfrog.trajectory_intelligence.queries`) and the
current, in-progress review's own already-computed changed surfaces
(Change Intelligence's own ``ChangeUnit.changed_candidates``, computed
earlier in the same run -- never re-derived here).
"""

from __future__ import annotations

import uuid

from patchfrog.indexing.inventory import is_test_path
from patchfrog.trajectory_intelligence.domain import (
    MAX_EVENTS_PER_SURFACE,
    MAX_TRAJECTORY_EVENTS,
    MAX_TRAJECTORY_SIGNALS,
    MIN_SURFACE_CHURN_EVENTS,
    TrajectoryEvent,
    TrajectoryEventKind,
    TrajectoryHead,
    TrajectoryReviewHint,
    TrajectorySignal,
    TrajectorySignalKind,
)

#: The only mapping any v1 signal kind ever uses -- see the domain
#: module's own docstring for why `DEEPEN_CONTEXT`/
#: `INCREASE_CANDIDATE_PRIORITY` are reserved, never selected here.
_SIGNAL_HINT: dict[TrajectorySignalKind, TrajectoryReviewHint] = {
    TrajectorySignalKind.REPEATED_SURFACE_CHURN: TrajectoryReviewHint.REQUIRE_CRITIC,
}


def _event_kind_for(file_path: str) -> TrajectoryEventKind:
    return TrajectoryEventKind.TEST_SURFACE_CHANGED if is_test_path(file_path) else TrajectoryEventKind.SURFACE_CHANGED


def derive_trajectory_events(
    *,
    heads: tuple[TrajectoryHead, ...],
    historical_surfaces_by_review_run: dict[uuid.UUID, tuple[tuple[str, str], ...]],
    current_changed_surfaces: tuple[tuple[str, str], ...] = (),
) -> tuple[TrajectoryEvent, ...]:
    """``heads`` is oldest-first -- the exact, already-decided lineage
    :mod:`patchfrog.trajectory_intelligence.service` chose to use for
    this run (real persisted heads, optionally followed by one
    synthetic in-progress head with ``review_run_id=None``; see that
    module's own docstring for the current-edge verification this
    depends on). A head with a real ``review_run_id`` always reads its
    surfaces from ``historical_surfaces_by_review_run`` (the persisted
    truth for that exact head, never re-derived); a head with
    ``review_run_id=None`` (there is ever at most one, and only ever
    the last) reads from ``current_changed_surfaces`` instead -- Change
    Intelligence's own already-computed current-run surfaces, since
    that head's own review hasn't persisted a generation yet."""

    events: list[TrajectoryEvent] = []
    per_surface_count: dict[tuple[str, str], int] = {}

    def _add(head: TrajectoryHead, file_path: str, qualified_name: str) -> bool:
        if len(events) >= MAX_TRAJECTORY_EVENTS:
            return False
        key = (file_path, qualified_name)
        count = per_surface_count.get(key, 0)
        if count >= MAX_EVENTS_PER_SURFACE:
            return True
        per_surface_count[key] = count + 1
        events.append(TrajectoryEvent(head=head, file_path=file_path, qualified_name=qualified_name, event_kind=_event_kind_for(file_path)))
        return True

    for head in heads:
        surfaces = (
            historical_surfaces_by_review_run.get(head.review_run_id, ())
            if head.review_run_id is not None
            else current_changed_surfaces
        )
        for file_path, qualified_name in surfaces:
            if not _add(head, file_path, qualified_name):
                return tuple(events)

    return tuple(events)


def derive_trajectory_signals(events: tuple[TrajectoryEvent, ...]) -> tuple[TrajectorySignal, ...]:
    """`REPEATED_SURFACE_CHURN`: the exact same surface touched across
    at least :data:`~patchfrog.trajectory_intelligence.domain.MIN_SURFACE_CHURN_EVENTS`
    *distinct* heads (deduplicated by ``commit_sha`` -- two events from
    the same head, however unlikely given the query's own dedup, would
    never inflate this count)."""

    by_surface: dict[tuple[str, str], list[TrajectoryEvent]] = {}
    for event in events:
        by_surface.setdefault((event.file_path, event.qualified_name), []).append(event)

    signals: list[TrajectorySignal] = []
    for (file_path, qualified_name), surface_events in by_surface.items():
        distinct_commit_shas = {e.head.commit_sha for e in surface_events}
        if len(distinct_commit_shas) < MIN_SURFACE_CHURN_EVENTS:
            continue

        signal_kind = TrajectorySignalKind.REPEATED_SURFACE_CHURN
        evidence = (
            f"this exact surface changed across {len(distinct_commit_shas)} distinct heads "
            f"in the current PR's own lineage"
        )
        signals.append(
            TrajectorySignal(
                surface_file_path=file_path, surface_qualified_name=qualified_name, signal_kind=signal_kind,
                supporting_events=tuple(surface_events[:MAX_EVENTS_PER_SURFACE]),
                review_hint=_SIGNAL_HINT[signal_kind], evidence=evidence,
            )
        )
        if len(signals) >= MAX_TRAJECTORY_SIGNALS:
            break

    return tuple(signals)


def select_review_hint(
    signals: tuple[TrajectorySignal, ...], *, file_path: str, qualified_name: str | None
) -> TrajectoryReviewHint:
    """The one deterministic hint a real current-PR candidate receives
    -- never an LLM decision. ``qualified_name=None`` never matches
    (module-region candidates carry no trajectory signal in v1, same
    exclusion as candidate-identity everywhere else in this package)."""

    if qualified_name is None:
        return TrajectoryReviewHint.NONE
    for signal in signals:
        if signal.surface_file_path == file_path and signal.surface_qualified_name == qualified_name:
            return signal.review_hint
    return TrajectoryReviewHint.NONE
