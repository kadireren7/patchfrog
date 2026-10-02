"""Manually submitted upstream snapshots (M11.2): the "I know about this
change" path. Nothing is fetched; the caller supplies a contract document or a
version, and it flows through the same snapshot -> diff -> event pipeline."""

from __future__ import annotations

from datetime import datetime

from patchfrog.upstream.domain import DependencyRelease
from patchfrog.upstream.events import LoadedContract
from patchfrog.watchers.domain import SnapshotKind, WatcherSnapshot, WatcherSource


def snapshot_from_contract(source: WatcherSource, contract: LoadedContract, *, now: datetime) -> WatcherSnapshot:
    return WatcherSnapshot(
        source_key=source.key, kind=SnapshotKind.CONTRACT, fetched_at=now, latest_version=contract.version,
        contract=contract,
    )


def snapshot_from_version(
    source: WatcherSource, version: str, *, now: datetime, release: DependencyRelease | None = None
) -> WatcherSnapshot:
    return WatcherSnapshot(
        source_key=source.key, kind=SnapshotKind.VERSION, fetched_at=now, latest_version=version,
        release=release or DependencyRelease(version=version[:64]),
    )


__all__ = ["snapshot_from_contract", "snapshot_from_version"]
