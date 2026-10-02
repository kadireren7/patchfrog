"""OpenAPI-URL and changelog-feed adapters (M11.2).

*OpenAPI*: fetches a spec document and builds the same normalized contract
M5/M6 use, so a revision diffs structurally. Remote ``$ref`` documents are
**not** resolved (only the fetched document is read).

*Changelog feed*: reads Atom, RSS or JSON Feed only -- structured feeds, never
HTML scraping. A feed entry whose title carries a version becomes a version
signal; one without a version is ignored (a changelog sentence is not proof of
anything). The XML parser rejects DTDs/entities outright.
"""

from __future__ import annotations

import hashlib
import json
import xml.etree.ElementTree as ET
from datetime import datetime
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from patchfrog.dependencies.openapi import load_spec
from patchfrog.upstream.domain import DependencyRelease
from patchfrog.upstream.events import (
    ContractLoadError,
    parse_contract_document,
    release_from_metadata,
)
from patchfrog.watchers.adapters.base import conditional_headers, pick_latest
from patchfrog.watchers.domain import (
    SnapshotKind,
    WatcherCursor,
    WatcherSnapshot,
    WatcherSource,
    WatcherSourceKind,
)
from patchfrog.watchers.fetch import Fetcher, PermanentFetchError, raise_for_response

MAX_SPEC_BYTES = 5_000_000
MAX_FEED_BYTES = 1_000_000
MAX_FEED_ENTRIES = 50


def _display_ref(url: str) -> str:
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.hostname or "", parts.path[:256], "", ""))


class OpenApiUrlAdapter:
    kind = WatcherSourceKind.OPENAPI_URL

    async def fetch(
        self, source: WatcherSource, cursor: WatcherCursor, fetcher: Fetcher, *, now: datetime
    ) -> WatcherSnapshot:
        url = source.normalized_locator
        response = await fetcher.get(url, headers=conditional_headers(cursor), max_bytes=MAX_SPEC_BYTES)
        raise_for_response(response, what=f"OpenAPI {_display_ref(url)}")
        if response.not_modified:
            return WatcherSnapshot(
                source_key=source.key, kind=SnapshotKind.CONTRACT, fetched_at=now, etag=cursor.etag, not_modified=True
            )
        try:
            text = response.body.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise PermanentFetchError("OpenAPI: response is not UTF-8 text") from exc
        spec = load_spec(text)
        if spec is None:
            raise PermanentFetchError("OpenAPI: the document is not an OpenAPI 3.x / Swagger 2.0 specification")
        try:
            contract = parse_contract_document(spec, ref=_display_ref(url))
        except ContractLoadError as exc:
            raise PermanentFetchError(str(exc)) from exc
        return WatcherSnapshot(
            source_key=source.key, kind=SnapshotKind.CONTRACT, fetched_at=now, latest_version=contract.version,
            contract=contract, etag=response.etag,
        )


def _entries_from_json_feed(data: Any) -> list[dict[str, str]]:
    items = data.get("items") if isinstance(data, dict) else None
    out: list[dict[str, str]] = []
    for item in items if isinstance(items, list) else []:
        if isinstance(item, dict):
            out.append(
                {
                    "title": str(item.get("title") or ""), "url": str(item.get("url") or ""),
                    "published": str(item.get("date_published") or ""),
                    "body": str(item.get("content_text") or item.get("content_html") or ""),
                }
            )
    return out


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _entries_from_xml(body: bytes) -> list[dict[str, str]]:
    head = body[:4096].lower()
    if b"<!doctype" in head or b"<!entity" in body[:65536].lower():
        raise PermanentFetchError("feed: DTD/entity declarations are not allowed")
    try:
        root = ET.fromstring(body)
    except ET.ParseError as exc:
        raise PermanentFetchError("feed: not well-formed XML") from exc
    out: list[dict[str, str]] = []
    for node in root.iter():
        if _local(node.tag) not in ("entry", "item"):
            continue
        fields: dict[str, str] = {"title": "", "url": "", "published": "", "body": ""}
        for child in node:
            name = _local(child.tag)
            if name == "title":
                fields["title"] = (child.text or "").strip()
            elif name == "link":
                fields["url"] = child.attrib.get("href") or (child.text or "").strip()
            elif name in ("published", "updated", "pubDate") and not fields["published"]:
                fields["published"] = (child.text or "").strip()
            elif name in ("summary", "content", "description"):
                fields["body"] = (child.text or "").strip()
        out.append(fields)
    return out


class ChangelogFeedAdapter:
    kind = WatcherSourceKind.CHANGELOG_FEED

    async def fetch(
        self, source: WatcherSource, cursor: WatcherCursor, fetcher: Fetcher, *, now: datetime
    ) -> WatcherSnapshot:
        url = source.normalized_locator
        response = await fetcher.get(url, headers=conditional_headers(cursor), max_bytes=MAX_FEED_BYTES)
        raise_for_response(response, what=f"feed {_display_ref(url)}")
        if response.not_modified:
            return WatcherSnapshot(
                source_key=source.key, kind=SnapshotKind.VERSION, fetched_at=now, etag=cursor.etag, not_modified=True
            )
        stripped = response.body.lstrip()
        if stripped.startswith(b"{"):
            try:
                entries = _entries_from_json_feed(json.loads(stripped.decode("utf-8")))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise PermanentFetchError("feed: not valid JSON Feed") from exc
        else:
            entries = _entries_from_xml(stripped)
        releases: dict[str, DependencyRelease] = {}
        for entry in entries[:MAX_FEED_ENTRIES]:
            try:
                release = release_from_metadata(
                    {"tag": entry["title"], "published_at": entry["published"] or None, "url": entry["url"],
                     "notes": entry["body"]}
                )
            except ContractLoadError:
                continue
            releases.setdefault(release.version, release)
        latest = pick_latest(releases, include_prereleases=source.include_prereleases)
        if latest is None:
            return WatcherSnapshot(
                source_key=source.key, kind=SnapshotKind.VERSION, fetched_at=now, etag=response.etag,
                notes=("no version-bearing entry found",),
            )
        digest = hashlib.sha256(latest.encode()).hexdigest()[:12]
        return WatcherSnapshot(
            source_key=source.key, kind=SnapshotKind.VERSION, fetched_at=now, latest_version=latest,
            release=releases[latest], etag=response.etag, notes=(f"feed latest {digest}",),
        )


__all__ = ["MAX_FEED_BYTES", "MAX_SPEC_BYTES", "ChangelogFeedAdapter", "OpenApiUrlAdapter"]
