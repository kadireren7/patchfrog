"""Pure domain model for continuous upstream watching (M11.3).

A *watcher* turns "something upstream might have changed" into at most one
:class:`~patchfrog.upstream.domain.ExternalChangeEvent` -- the same event
type M6 builds from manual input -- so a detected change flows into exactly
the M6 -> M10 -> M7 -> M8 -> M9 pipeline a hand-fed change does.

Responsibilities that live here (public engine): fetch -> normalize ->
fingerprint -> compare with the prior snapshot -> emit a candidate change ->
avoid duplicates. Responsibilities that do **not**: *when* to poll, retry
scheduling, cursor storage, rate-limit state across workers -- those operate
the hosted service (Cloud).

A :class:`WatcherCursor` is the only state a watcher needs between polls. It
serializes to a small JSON document the hosting service stores opaquely.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from patchfrog.upstream.domain import (
    DependencyRelease,
    DependencyTarget,
    ExternalChangeEvent,
    normalize_package_name,
)
from patchfrog.upstream.events import LoadedContract

#: Bumped when normalization, snapshot comparison or cursor semantics change.
WATCHER_ENGINE_VERSION = 1
CURSOR_FORMAT_VERSION = 1
#: Fingerprints of recently emitted events, kept to suppress duplicates.
MAX_EMITTED_FINGERPRINTS = 50
#: A stored previous contract larger than this is kept as a fingerprint only.
MAX_CURSOR_CONTRACT_BYTES = 256_000


class WatcherSourceKind(StrEnum):
    PYPI = "pypi"
    NPM = "npm"
    GITHUB_RELEASES = "github_releases"
    OPENAPI_URL = "openapi_url"
    CHANGELOG_FEED = "changelog_feed"
    MANUAL = "manual"


#: Source kinds whose evidence is a version only (no structural contract).
VERSION_ONLY_KINDS = frozenset(
    {WatcherSourceKind.PYPI, WatcherSourceKind.NPM, WatcherSourceKind.GITHUB_RELEASES, WatcherSourceKind.CHANGELOG_FEED}
)


@dataclass(frozen=True, slots=True)
class WatcherSource:
    """One upstream thing to watch, plus the dependency identity a change to
    it refers to (what consumers are matched against)."""

    kind: WatcherSourceKind
    locator: str
    target: DependencyTarget
    include_prereleases: bool = False

    @property
    def normalized_locator(self) -> str:
        text = self.locator.strip()
        if self.kind is WatcherSourceKind.PYPI:
            return normalize_package_name(text)
        if self.kind is WatcherSourceKind.NPM:
            return text.lower()
        if self.kind is WatcherSourceKind.GITHUB_RELEASES:
            return text.strip("/").lower()
        if self.kind in (WatcherSourceKind.OPENAPI_URL, WatcherSourceKind.CHANGELOG_FEED):
            return text.split("#", 1)[0]
        return text

    @property
    def key(self) -> str:
        """Stable identity for de-duplicating fetches across workspaces."""

        return f"{self.kind.value}:{self.normalized_locator}"


class SnapshotKind(StrEnum):
    VERSION = "version"
    CONTRACT = "contract"


@dataclass(frozen=True, slots=True)
class WatcherSnapshot:
    """The normalized result of one successful fetch."""

    source_key: str
    kind: SnapshotKind
    fetched_at: datetime
    latest_version: str | None = None
    release: DependencyRelease | None = None
    #: In memory only; only its fingerprint (and bounded JSON) enter the cursor.
    contract: LoadedContract | None = None
    etag: str | None = None
    #: The upstream said "not modified" -- nothing to compare.
    not_modified: bool = False
    notes: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class WatcherCursor:
    """Everything a watcher remembers between polls."""

    source_key: str
    last_version: str | None = None
    last_contract_fingerprint: str | None = None
    last_contract_json: str | None = None
    last_contract_ref: str | None = None
    last_contract_version: str | None = None
    etag: str | None = None
    emitted: tuple[str, ...] = ()
    observations: int = 0
    last_observed_at: datetime | None = None

    @classmethod
    def empty(cls, source_key: str) -> WatcherCursor:
        return cls(source_key=source_key)

    @property
    def has_baseline(self) -> bool:
        return self.last_version is not None or self.last_contract_fingerprint is not None

    def to_json(self) -> str:
        return json.dumps(
            {
                "v": CURSOR_FORMAT_VERSION, "source_key": self.source_key, "last_version": self.last_version,
                "last_contract_fingerprint": self.last_contract_fingerprint,
                "last_contract_json": self.last_contract_json, "last_contract_ref": self.last_contract_ref,
                "last_contract_version": self.last_contract_version, "etag": self.etag,
                "emitted": list(self.emitted), "observations": self.observations,
                "last_observed_at": self.last_observed_at.isoformat() if self.last_observed_at else None,
            },
            sort_keys=True,
        )

    @classmethod
    def from_json(cls, raw: str) -> WatcherCursor:
        data: dict[str, Any] = json.loads(raw)
        if data.get("v") != CURSOR_FORMAT_VERSION:
            raise ValueError(f"unsupported watcher cursor format {data.get('v')!r}")
        observed = data.get("last_observed_at")
        return cls(
            source_key=str(data["source_key"]), last_version=data.get("last_version"),
            last_contract_fingerprint=data.get("last_contract_fingerprint"),
            last_contract_json=data.get("last_contract_json"), last_contract_ref=data.get("last_contract_ref"),
            last_contract_version=data.get("last_contract_version"), etag=data.get("etag"),
            emitted=tuple(data.get("emitted", ())), observations=int(data.get("observations", 0)),
            last_observed_at=(datetime.fromisoformat(observed).astimezone(UTC) if observed else None),
        )


@dataclass(frozen=True, slots=True)
class DetectedUpstreamChange:
    source_key: str
    source_kind: WatcherSourceKind
    event: ExternalChangeEvent
    previous_version: str | None
    new_version: str | None

    @property
    def fingerprint(self) -> str:
        return self.event.fingerprint


class WatchOutcomeKind(StrEnum):
    #: First observation: baseline recorded, nothing emitted (history is never replayed).
    BASELINE = "baseline"
    UNCHANGED = "unchanged"
    CHANGED = "changed"
    DUPLICATE_SUPPRESSED = "duplicate_suppressed"
    #: A change was seen but cannot become an event (e.g. unparseable version).
    SKIPPED = "skipped"


@dataclass(frozen=True, slots=True)
class WatchOutcome:
    kind: WatchOutcomeKind
    cursor: WatcherCursor
    change: DetectedUpstreamChange | None = None
    notes: tuple[str, ...] = field(default_factory=tuple)


__all__ = [
    "CURSOR_FORMAT_VERSION",
    "MAX_CURSOR_CONTRACT_BYTES",
    "MAX_EMITTED_FINGERPRINTS",
    "VERSION_ONLY_KINDS",
    "WATCHER_ENGINE_VERSION",
    "DetectedUpstreamChange",
    "SnapshotKind",
    "WatchOutcome",
    "WatchOutcomeKind",
    "WatcherCursor",
    "WatcherSnapshot",
    "WatcherSource",
    "WatcherSourceKind",
]
