"""Continuous upstream watching (M11): detection, normalization and diff
intelligence. Hosted scheduling lives in PatchFrog Cloud."""

from __future__ import annotations

from patchfrog.watchers.domain import (
    WATCHER_ENGINE_VERSION,
    DetectedUpstreamChange,
    WatcherCursor,
    WatcherSnapshot,
    WatcherSource,
    WatcherSourceKind,
    WatchOutcome,
    WatchOutcomeKind,
)

__all__ = [
    "WATCHER_ENGINE_VERSION",
    "DetectedUpstreamChange",
    "WatchOutcome",
    "WatchOutcomeKind",
    "WatcherCursor",
    "WatcherSnapshot",
    "WatcherSource",
    "WatcherSourceKind",
]
