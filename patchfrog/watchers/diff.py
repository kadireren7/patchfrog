"""Snapshot comparison: ``(source, new snapshot, prior cursor) -> outcome``
(M11.3).

Deterministic and side-effect free. The same inputs always yield the same
outcome, and the emitted event's fingerprint is the M6 event fingerprint, so
the same upstream change observed by two pollers (or twice) is one event.

First observation records a **baseline** and emits nothing: a watcher never
replays history it was not watching.
"""

from __future__ import annotations

import json
from dataclasses import replace

from patchfrog.upstream.domain import ExternalChangeEvent, ExternalChangeSource
from patchfrog.upstream.events import (
    ContractLoadError,
    LoadedContract,
    build_contract_change,
    build_version_change,
    contract_from_registry_snapshot,
)
from patchfrog.upstream.hints import EMPTY_HINTS, ChangeHints
from patchfrog.upstream.package_version import parse_version
from patchfrog.watchers.adapters.base import version_key
from patchfrog.watchers.domain import (
    MAX_CURSOR_CONTRACT_BYTES,
    MAX_EMITTED_FINGERPRINTS,
    DetectedUpstreamChange,
    SnapshotKind,
    WatcherCursor,
    WatcherSnapshot,
    WatcherSource,
    WatcherSourceKind,
    WatchOutcome,
    WatchOutcomeKind,
)

_VERSION_SOURCE = {
    WatcherSourceKind.PYPI: ExternalChangeSource.PACKAGE_VERSION,
    WatcherSourceKind.NPM: ExternalChangeSource.PACKAGE_VERSION,
    WatcherSourceKind.MANUAL: ExternalChangeSource.PACKAGE_VERSION,
    WatcherSourceKind.GITHUB_RELEASES: ExternalChangeSource.GITHUB_RELEASE,
    WatcherSourceKind.CHANGELOG_FEED: ExternalChangeSource.CHANGELOG,
}


def _contract_json(contract: LoadedContract) -> str | None:
    rendered = json.dumps(contract.normalized, sort_keys=True, separators=(",", ":"), default=str)
    return rendered if len(rendered.encode()) <= MAX_CURSOR_CONTRACT_BYTES else None


def _remember(cursor: WatcherCursor, fingerprint: str) -> tuple[str, ...]:
    return (*[f for f in cursor.emitted if f != fingerprint], fingerprint)[-MAX_EMITTED_FINGERPRINTS:]


def _detected(source: WatcherSource, event: ExternalChangeEvent) -> DetectedUpstreamChange:
    return DetectedUpstreamChange(
        source_key=source.key, source_kind=source.kind, event=event, previous_version=event.old.version,
        new_version=event.new.version,
    )


def detect_change(
    source: WatcherSource, snapshot: WatcherSnapshot, cursor: WatcherCursor, *, hints: ChangeHints = EMPTY_HINTS
) -> WatchOutcome:
    base = replace(
        cursor, observations=cursor.observations + 1, last_observed_at=snapshot.fetched_at,
        etag=snapshot.etag or cursor.etag,
    )
    if snapshot.not_modified:
        return WatchOutcome(WatchOutcomeKind.UNCHANGED, base, notes=("upstream reported not modified",))
    if snapshot.kind is SnapshotKind.VERSION:
        return _detect_version(source, snapshot, base)
    return _detect_contract(source, snapshot, base, hints)


def _detect_version(source: WatcherSource, snapshot: WatcherSnapshot, cursor: WatcherCursor) -> WatchOutcome:
    latest = snapshot.latest_version
    if latest is None:
        return WatchOutcome(WatchOutcomeKind.UNCHANGED, cursor, notes=snapshot.notes or ("no version observed",))
    new = parse_version(latest)
    if new is None:
        return WatchOutcome(WatchOutcomeKind.SKIPPED, cursor, notes=(f"unparseable version {latest[:40]!r}",))
    if cursor.last_version is None:
        return WatchOutcome(WatchOutcomeKind.BASELINE, replace(cursor, last_version=latest))
    old = parse_version(cursor.last_version)
    if old is None:
        return WatchOutcome(WatchOutcomeKind.BASELINE, replace(cursor, last_version=latest),
                            notes=("previous version was unparseable; baseline reset",))
    if version_key(new) == version_key(old):
        return WatchOutcome(WatchOutcomeKind.UNCHANGED, cursor)
    if version_key(new) < version_key(old):
        return WatchOutcome(WatchOutcomeKind.UNCHANGED, cursor,
                            notes=("upstream latest decreased (yank/rollback); cursor kept at the higher version",))
    event = build_version_change(
        source.target, cursor.last_version, latest, source=_VERSION_SOURCE[source.kind], release=snapshot.release,
        observed_at=snapshot.fetched_at,
    )
    return _emit(source, cursor, event, new_version=latest)


def _detect_contract(
    source: WatcherSource, snapshot: WatcherSnapshot, cursor: WatcherCursor, hints: ChangeHints
) -> WatchOutcome:
    contract = snapshot.contract
    if contract is None:
        return WatchOutcome(WatchOutcomeKind.SKIPPED, cursor, notes=("contract snapshot carried no contract",))
    remembered = replace(
        cursor, last_contract_fingerprint=contract.fingerprint, last_contract_json=_contract_json(contract),
        last_contract_ref=contract.ref, last_contract_version=contract.version, last_version=contract.version or cursor.last_version,
    )
    if cursor.last_contract_fingerprint is None:
        return WatchOutcome(WatchOutcomeKind.BASELINE, remembered)
    if cursor.last_contract_fingerprint == contract.fingerprint:
        return WatchOutcome(WatchOutcomeKind.UNCHANGED, replace(cursor, last_contract_version=contract.version))
    if cursor.last_contract_json is None:
        return WatchOutcome(
            WatchOutcomeKind.SKIPPED, remembered,
            notes=("the previous contract was too large to keep; the change cannot be diffed structurally",),
        )
    try:
        old = contract_from_registry_snapshot(
            cursor.last_contract_json, fingerprint=cursor.last_contract_fingerprint,
            ref=cursor.last_contract_ref or "previous", version=cursor.last_contract_version,
        )
        event = build_contract_change(
            old, contract, hints=hints, target=source.target,
            source=ExternalChangeSource.MANUAL_CONTRACT_PAIR if source.kind is WatcherSourceKind.MANUAL else None,
            release=snapshot.release, observed_at=snapshot.fetched_at,
        )
    except ContractLoadError as exc:
        return WatchOutcome(WatchOutcomeKind.SKIPPED, remembered, notes=(f"cannot diff: {exc}"[:300],))
    return _emit(source, remembered, event, new_version=contract.version)


def _emit(source: WatcherSource, cursor: WatcherCursor, event: ExternalChangeEvent, *, new_version: str | None) -> WatchOutcome:
    advanced = replace(cursor, last_version=new_version or cursor.last_version)
    if event.fingerprint in cursor.emitted:
        return WatchOutcome(WatchOutcomeKind.DUPLICATE_SUPPRESSED, advanced, notes=("this change was already emitted",))
    advanced = replace(advanced, emitted=_remember(cursor, event.fingerprint))
    return WatchOutcome(WatchOutcomeKind.CHANGED, advanced, change=_detected(source, event))


__all__ = ["detect_change"]
