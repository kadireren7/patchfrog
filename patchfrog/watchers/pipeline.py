"""One poll of one source: fetch -> normalize -> compare -> outcome (M11.5).

``poll_source`` is the engine half of "scheduled watcher -> snapshot ->
fingerprint comparison -> change event". The hosted service decides *when*
to call it, persists the returned cursor, retries on
:class:`~patchfrog.watchers.fetch.WatcherError` and hands a detected change to
the campaign pipeline (:func:`patchfrog.campaigns.orchestrate.run_campaign`).
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime

from patchfrog.upstream.hints import EMPTY_HINTS, ChangeHints
from patchfrog.watchers.adapters import SourceAdapter, default_adapters
from patchfrog.watchers.diff import detect_change
from patchfrog.watchers.domain import (
    WatcherCursor,
    WatcherSnapshot,
    WatcherSource,
    WatcherSourceKind,
    WatchOutcome,
)
from patchfrog.watchers.fetch import Fetcher, PermanentFetchError


async def poll_source(
    source: WatcherSource,
    cursor: WatcherCursor | None,
    *,
    fetcher: Fetcher,
    now: datetime,
    adapters: Mapping[WatcherSourceKind, SourceAdapter] | None = None,
    hints: ChangeHints = EMPTY_HINTS,
) -> WatchOutcome:
    """Raises a :class:`~patchfrog.watchers.fetch.WatcherError` subclass on a
    fetch failure -- the cursor is then unchanged and the caller schedules a retry."""

    registry = adapters if adapters is not None else default_adapters()
    adapter = registry.get(source.kind)
    if adapter is None:
        raise PermanentFetchError(f"no adapter for source kind {source.kind.value!r} (manual sources are submitted, not polled)")
    current = cursor or WatcherCursor.empty(source.key)
    snapshot = await adapter.fetch(source, current, fetcher, now=now)
    return detect_change(source, snapshot, current, hints=hints)


def ingest_snapshot(
    source: WatcherSource, snapshot: WatcherSnapshot, cursor: WatcherCursor | None, *, hints: ChangeHints = EMPTY_HINTS
) -> WatchOutcome:
    """The manual path: a caller-supplied snapshot through the same comparison."""

    return detect_change(source, snapshot, cursor or WatcherCursor.empty(source.key), hints=hints)


__all__ = ["ingest_snapshot", "poll_source"]
