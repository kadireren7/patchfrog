"""Shared helpers for watcher source adapters."""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from datetime import datetime
from typing import Any, Protocol

from patchfrog.upstream.package_version import ParsedVersion, parse_version
from patchfrog.watchers.domain import (
    WatcherCursor,
    WatcherSnapshot,
    WatcherSource,
    WatcherSourceKind,
)
from patchfrog.watchers.fetch import Fetcher, PermanentFetchError


class SourceAdapter(Protocol):
    kind: WatcherSourceKind

    async def fetch(
        self, source: WatcherSource, cursor: WatcherCursor, fetcher: Fetcher, *, now: datetime
    ) -> WatcherSnapshot: ...


def version_key(parsed: ParsedVersion) -> tuple[int, int, int, int, str]:
    """A total order in which a final release outranks its own pre-releases."""

    return (parsed.major, parsed.minor, parsed.patch, 1 if parsed.prerelease is None else 0, parsed.prerelease or "")


def pick_latest(versions: Iterable[str], *, include_prereleases: bool) -> str | None:
    best: tuple[tuple[int, int, int, int, str], str] | None = None
    for raw in versions:
        parsed = parse_version(raw)
        if parsed is None or (parsed.prerelease is not None and not include_prereleases):
            continue
        key = version_key(parsed)
        if best is None or key > best[0]:
            best = (key, raw)
    return best[1] if best else None


def json_object(body: bytes, *, what: str) -> Mapping[str, Any]:
    try:
        data = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PermanentFetchError(f"{what}: response is not valid JSON") from exc
    if not isinstance(data, dict):
        raise PermanentFetchError(f"{what}: unexpected JSON shape")
    return data


def conditional_headers(cursor: WatcherCursor) -> dict[str, str]:
    return {"If-None-Match": cursor.etag} if cursor.etag else {}


__all__ = ["SourceAdapter", "conditional_headers", "json_object", "pick_latest", "version_key"]
