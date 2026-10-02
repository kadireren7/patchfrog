"""Package-registry adapters: PyPI and npm (M11.2). Both read the registries'
documented public JSON APIs -- no scraping."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from urllib.parse import quote

from patchfrog.upstream.domain import DependencyRelease
from patchfrog.upstream.package_version import parse_version
from patchfrog.watchers.adapters.base import conditional_headers, json_object, pick_latest
from patchfrog.watchers.domain import (
    SnapshotKind,
    WatcherCursor,
    WatcherSnapshot,
    WatcherSource,
    WatcherSourceKind,
)
from patchfrog.watchers.fetch import Fetcher, PermanentFetchError, raise_for_response

PYPI_BASE = "https://pypi.org"
NPM_BASE = "https://registry.npmjs.org"


def _not_modified(source: WatcherSource, now: datetime, etag: str | None) -> WatcherSnapshot:
    return WatcherSnapshot(source_key=source.key, kind=SnapshotKind.VERSION, fetched_at=now, etag=etag, not_modified=True)


class PyPIAdapter:
    kind = WatcherSourceKind.PYPI

    def __init__(self, *, base_url: str = PYPI_BASE) -> None:
        self._base = base_url.rstrip("/")

    async def fetch(
        self, source: WatcherSource, cursor: WatcherCursor, fetcher: Fetcher, *, now: datetime
    ) -> WatcherSnapshot:
        name = quote(source.normalized_locator, safe="")
        url = f"{self._base}/pypi/{name}/json"
        response = await fetcher.get(url, headers=conditional_headers(cursor))
        raise_for_response(response, what=f"PyPI {source.normalized_locator}")
        if response.not_modified:
            return _not_modified(source, now, cursor.etag)
        data = json_object(response.body, what="PyPI")
        releases = data.get("releases")
        if not isinstance(releases, dict):
            raise PermanentFetchError("PyPI: response has no 'releases'")
        usable: dict[str, str | None] = {}
        for version, files in releases.items():
            if not isinstance(files, list) or not files:
                continue
            live = [f for f in files if isinstance(f, dict) and not f.get("yanked")]
            if not live:
                continue
            times = sorted(str(f["upload_time_iso_8601"]) for f in live if f.get("upload_time_iso_8601"))
            usable[str(version)] = times[0] if times else None
        latest = pick_latest(usable, include_prereleases=source.include_prereleases)
        if latest is None:
            return WatcherSnapshot(
                source_key=source.key, kind=SnapshotKind.VERSION, fetched_at=now, etag=response.etag,
                notes=("no usable release found",),
            )
        release = DependencyRelease(
            version=latest[:64], published_at=usable[latest],
            url=f"{self._base}/project/{name}/{quote(latest, safe='')}/",
            prerelease=bool((parsed := parse_version(latest)) and parsed.prerelease),
        )
        return WatcherSnapshot(
            source_key=source.key, kind=SnapshotKind.VERSION, fetched_at=now, latest_version=latest, release=release,
            etag=response.etag,
        )


class NpmAdapter:
    kind = WatcherSourceKind.NPM

    def __init__(self, *, base_url: str = NPM_BASE) -> None:
        self._base = base_url.rstrip("/")

    async def fetch(
        self, source: WatcherSource, cursor: WatcherCursor, fetcher: Fetcher, *, now: datetime
    ) -> WatcherSnapshot:
        name = source.normalized_locator
        # scoped packages keep a literal "@" and encode the single "/" (the registry's documented form)
        url = f"{self._base}/{name.replace('/', '%2F', 1) if name.startswith('@') else quote(name, safe='')}"
        response = await fetcher.get(url, headers=conditional_headers(cursor))
        raise_for_response(response, what=f"npm {name}")
        if response.not_modified:
            return _not_modified(source, now, cursor.etag)
        data = json_object(response.body, what="npm")
        versions = data.get("versions")
        if not isinstance(versions, dict):
            raise PermanentFetchError("npm: response has no 'versions'")
        tags: Any = data.get("dist-tags")
        tagged = tags.get("latest") if isinstance(tags, dict) else None
        live = {v for v, meta in versions.items() if not (isinstance(meta, dict) and meta.get("deprecated"))}
        latest = None
        if not source.include_prereleases and isinstance(tagged, str) and tagged in live:
            latest = pick_latest([tagged], include_prereleases=False)
        if latest is None:
            latest = pick_latest(live, include_prereleases=source.include_prereleases)
        if latest is None:
            return WatcherSnapshot(
                source_key=source.key, kind=SnapshotKind.VERSION, fetched_at=now, etag=response.etag,
                notes=("no usable release found",),
            )
        times: Any = data.get("time")
        published = times.get(latest) if isinstance(times, dict) else None
        release = DependencyRelease(
            version=latest[:64], published_at=str(published)[:64] if published else None,
            url=f"https://www.npmjs.com/package/{name}/v/{quote(latest, safe='')}",
        )
        return WatcherSnapshot(
            source_key=source.key, kind=SnapshotKind.VERSION, fetched_at=now, latest_version=latest, release=release,
            etag=response.etag,
        )


__all__ = ["NPM_BASE", "PYPI_BASE", "NpmAdapter", "PyPIAdapter"]
