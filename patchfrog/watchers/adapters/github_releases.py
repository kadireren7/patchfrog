"""GitHub Releases adapter (M11.2): the documented REST releases endpoint."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from urllib.parse import quote

from patchfrog.upstream.events import ContractLoadError, release_from_metadata
from patchfrog.watchers.adapters.base import conditional_headers, pick_latest
from patchfrog.watchers.domain import (
    SnapshotKind,
    WatcherCursor,
    WatcherSnapshot,
    WatcherSource,
    WatcherSourceKind,
)
from patchfrog.watchers.fetch import Fetcher, PermanentFetchError, raise_for_response

GITHUB_API = "https://api.github.com"
_PER_PAGE = 30


class GitHubReleasesAdapter:
    kind = WatcherSourceKind.GITHUB_RELEASES

    def __init__(self, *, api_base_url: str = GITHUB_API) -> None:
        self._base = api_base_url.rstrip("/")

    async def fetch(
        self, source: WatcherSource, cursor: WatcherCursor, fetcher: Fetcher, *, now: datetime
    ) -> WatcherSnapshot:
        locator = source.normalized_locator
        owner, _, repo = locator.partition("/")
        if not owner or not repo or "/" in repo:
            raise PermanentFetchError(f"GitHub releases source must be 'owner/repo', got {source.locator!r}")
        url = f"{self._base}/repos/{quote(owner, safe='')}/{quote(repo, safe='')}/releases?per_page={_PER_PAGE}"
        headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
        headers.update(conditional_headers(cursor))
        response = await fetcher.get(url, headers=headers)
        raise_for_response(response, what=f"GitHub releases {locator}")
        if response.not_modified:
            return WatcherSnapshot(
                source_key=source.key, kind=SnapshotKind.VERSION, fetched_at=now, etag=cursor.etag, not_modified=True
            )
        import json

        try:
            data: Any = json.loads(response.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise PermanentFetchError("GitHub releases: response is not valid JSON") from exc
        if not isinstance(data, list):
            raise PermanentFetchError("GitHub releases: unexpected response shape")
        by_version: dict[str, dict[str, Any]] = {}
        for item in data:
            if not isinstance(item, dict) or item.get("draft"):
                continue
            if item.get("prerelease") and not source.include_prereleases:
                continue
            try:
                release = release_from_metadata(item)
            except ContractLoadError:
                continue
            by_version.setdefault(release.version, item)
        latest = pick_latest(by_version, include_prereleases=source.include_prereleases)
        if latest is None:
            return WatcherSnapshot(
                source_key=source.key, kind=SnapshotKind.VERSION, fetched_at=now, etag=response.etag,
                notes=("no usable release found",),
            )
        return WatcherSnapshot(
            source_key=source.key, kind=SnapshotKind.VERSION, fetched_at=now, latest_version=latest,
            release=release_from_metadata(by_version[latest]), etag=response.etag,
        )


__all__ = ["GITHUB_API", "GitHubReleasesAdapter"]
