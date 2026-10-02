"""M11.2/M11.3: source adapters against the in-memory fetcher -- no network."""

from __future__ import annotations

import json
from datetime import timedelta

import pytest

from patchfrog.dependencies.domain import Ecosystem
from patchfrog.upstream.domain import DependencyTarget
from patchfrog.watchers.adapters import (
    ChangelogFeedAdapter,
    GitHubReleasesAdapter,
    NpmAdapter,
    OpenApiUrlAdapter,
    PyPIAdapter,
)
from patchfrog.watchers.domain import SnapshotKind, WatcherCursor, WatcherSource, WatcherSourceKind
from patchfrog.watchers.fetch import (
    FakeFetcher,
    FetchResponse,
    PermanentFetchError,
    RateLimitedError,
    TransientFetchError,
    backoff_delay_seconds,
    raise_for_response,
)
from tests.support.campaigns import NOW


def _source(kind: WatcherSourceKind, locator: str, **kw: object) -> WatcherSource:
    return WatcherSource(kind=kind, locator=locator, target=DependencyTarget(display_name=locator), **kw)  # type: ignore[arg-type]


def _json(data: object, **kw: object) -> FetchResponse:
    return FetchResponse(status=200, body=json.dumps(data).encode(), **kw)  # type: ignore[arg-type]


async def test_pypi_picks_latest_stable_and_ignores_prereleases_yanked_and_empty() -> None:
    source = _source(WatcherSourceKind.PYPI, "Acme_SDK")
    fetcher = FakeFetcher({
        "https://pypi.org/pypi/acme-sdk/json": _json({"releases": {
            "1.0.0": [{"upload_time_iso_8601": "2026-01-01T00:00:00Z"}],
            "1.5.0": [{"upload_time_iso_8601": "2026-03-01T00:00:00Z"}],
            "2.0.0rc1": [{"upload_time_iso_8601": "2026-04-01T00:00:00Z"}],
            "1.9.0": [{"upload_time_iso_8601": "2026-04-02T00:00:00Z", "yanked": True}],
            "1.8.0": [],
        }}, etag='"abc"'),
    })
    snap = await PyPIAdapter().fetch(source, WatcherCursor.empty(source.key), fetcher, now=NOW)
    assert snap.kind is SnapshotKind.VERSION and snap.latest_version == "1.5.0"
    assert snap.release is not None and snap.release.published_at == "2026-03-01T00:00:00Z"
    assert snap.release.url == "https://pypi.org/project/acme-sdk/1.5.0/" and not snap.release.prerelease
    assert snap.etag == '"abc"'


async def test_pypi_can_include_prereleases_and_a_final_outranks_its_own_prerelease() -> None:
    releases = {"2.0.0rc1": [{"upload_time_iso_8601": "t"}], "1.5.0": [{"upload_time_iso_8601": "t"}]}
    fetcher = FakeFetcher({"https://pypi.org/pypi/x/json": _json({"releases": releases})})
    include = _source(WatcherSourceKind.PYPI, "x", include_prereleases=True)
    snap = await PyPIAdapter().fetch(include, WatcherCursor.empty(include.key), fetcher, now=NOW)
    assert snap.latest_version == "2.0.0rc1" and snap.release is not None and snap.release.prerelease
    releases["2.0.0"] = [{"upload_time_iso_8601": "t"}]
    fetcher = FakeFetcher({"https://pypi.org/pypi/x/json": _json({"releases": releases})})
    final = await PyPIAdapter().fetch(include, WatcherCursor.empty(include.key), fetcher, now=NOW)
    assert final.latest_version == "2.0.0"


async def test_conditional_request_uses_etag_and_handles_304() -> None:
    source = _source(WatcherSourceKind.PYPI, "x")
    fetcher = FakeFetcher({"https://pypi.org/pypi/x/json": FetchResponse(status=304)})
    cursor = WatcherCursor(source_key=source.key, etag='"old"')
    snap = await PyPIAdapter().fetch(source, cursor, fetcher, now=NOW)
    assert snap.not_modified and snap.etag == '"old"'
    assert fetcher.header_names == [("If-None-Match",)]


async def test_npm_scoped_package_url_and_dist_tag_latest() -> None:
    source = _source(WatcherSourceKind.NPM, "@Acme/SDK")
    fetcher = FakeFetcher({
        "https://registry.npmjs.org/@acme%2Fsdk": _json({
            "dist-tags": {"latest": "3.1.0"},
            "versions": {"3.0.0": {}, "3.1.0": {}, "4.0.0-beta.1": {}},
            "time": {"3.1.0": "2026-05-05T00:00:00.000Z"},
        }),
    })
    snap = await NpmAdapter().fetch(source, WatcherCursor.empty(source.key), fetcher, now=NOW)
    assert snap.latest_version == "3.1.0" and snap.release is not None
    assert snap.release.published_at == "2026-05-05T00:00:00.000Z"


async def test_npm_skips_a_deprecated_latest_tag() -> None:
    source = _source(WatcherSourceKind.NPM, "pkg")
    fetcher = FakeFetcher({
        "https://registry.npmjs.org/pkg": _json({
            "dist-tags": {"latest": "2.0.0"}, "versions": {"1.9.0": {}, "2.0.0": {"deprecated": "do not use"}},
        }),
    })
    snap = await NpmAdapter().fetch(source, WatcherCursor.empty(source.key), fetcher, now=NOW)
    assert snap.latest_version == "1.9.0"


async def test_github_releases_ignore_drafts_and_prereleases_and_read_tags() -> None:
    source = _source(WatcherSourceKind.GITHUB_RELEASES, "Acme/SDK")
    url = "https://api.github.com/repos/acme/sdk/releases?per_page=30"
    fetcher = FakeFetcher({url: _json([
        {"tag_name": "v2.1.0", "draft": True, "published_at": "2026-06-01T00:00:00Z"},
        {"tag_name": "v2.0.0-rc.1", "prerelease": True},
        {"tag_name": "v1.9.0", "published_at": "2026-05-01T00:00:00Z", "html_url": "https://github.com/acme/sdk/releases/tag/v1.9.0",
         "body": "notes that must not be stored"},
        {"tag_name": "nightly"},
    ])})
    snap = await GitHubReleasesAdapter().fetch(source, WatcherCursor.empty(source.key), fetcher, now=NOW)
    assert snap.latest_version == "1.9.0" and snap.release is not None
    assert snap.release.notes_sha256 and "notes that must not" not in repr(snap.release)
    assert "Accept" in fetcher.header_names[0]


async def test_github_releases_rejects_a_malformed_locator() -> None:
    with pytest.raises(PermanentFetchError):
        await GitHubReleasesAdapter().fetch(
            _source(WatcherSourceKind.GITHUB_RELEASES, "just-a-name"), WatcherCursor.empty("k"), FakeFetcher(), now=NOW
        )


OPENAPI_V1 = {"openapi": "3.0.0", "info": {"title": "T", "version": "1.0.0"},
              "paths": {"/v1/items": {"get": {"parameters": [], "responses": {"200": {"description": "ok"}}}}}}


async def test_openapi_url_produces_a_normalized_contract_with_a_stable_fingerprint() -> None:
    url = "https://api.example.com/openapi.json"
    source = _source(WatcherSourceKind.OPENAPI_URL, url)
    fetcher = FakeFetcher({url: _json(OPENAPI_V1, etag='"e1"')})
    one = await OpenApiUrlAdapter().fetch(source, WatcherCursor.empty(source.key), fetcher, now=NOW)
    two = await OpenApiUrlAdapter().fetch(source, WatcherCursor.empty(source.key), fetcher, now=NOW + timedelta(hours=1))
    assert one.kind is SnapshotKind.CONTRACT and one.contract is not None and two.contract is not None
    assert one.contract.fingerprint == two.contract.fingerprint and one.latest_version == "1.0.0"
    assert one.contract.ref == url


async def test_openapi_url_rejects_non_openapi_and_oversized_documents() -> None:
    url = "https://x.example.com/spec"
    source = _source(WatcherSourceKind.OPENAPI_URL, url)
    with pytest.raises(PermanentFetchError, match="not an OpenAPI"):
        await OpenApiUrlAdapter().fetch(
            source, WatcherCursor.empty(source.key), FakeFetcher({url: _json({"hello": "world"})}), now=NOW
        )
    big = FetchResponse(status=200, body=b"x" * 5_000_001)
    with pytest.raises(PermanentFetchError, match="larger"):
        await OpenApiUrlAdapter().fetch(source, WatcherCursor.empty(source.key), FakeFetcher({url: big}), now=NOW)


ATOM = b"""<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom">
<entry><title>Release v2.3.0</title><link href="https://example.com/r/2.3.0"/><updated>2026-07-01T00:00:00Z</updated>
<summary>Breaking: removed foo()</summary></entry>
<entry><title>Release v2.2.0</title><link href="https://example.com/r/2.2.0"/><updated>2026-06-01T00:00:00Z</updated></entry>
<entry><title>Roadmap update (no version)</title></entry></feed>"""
RSS = b"""<?xml version="1.0"?><rss version="2.0"><channel><item><title>1.4.1 released</title><link>https://e.com/1</link>
<pubDate>Mon, 01 Jun 2026 00:00:00 GMT</pubDate></item></channel></rss>"""


async def test_changelog_feed_reads_atom_rss_and_json_feed() -> None:
    adapter = ChangelogFeedAdapter()
    for body, expected in (
        (ATOM, "2.3.0"), (RSS, "1.4.1"),
        (json.dumps({"items": [{"title": "v5.0.0", "url": "https://e.com/5"}, {"title": "v4.9.0"}]}).encode(), "5.0.0"),
    ):
        url = f"https://feeds.example.com/{expected}"
        source = _source(WatcherSourceKind.CHANGELOG_FEED, url)
        snap = await adapter.fetch(source, WatcherCursor.empty(source.key),
                                   FakeFetcher({url: FetchResponse(status=200, body=body)}), now=NOW)
        assert snap.latest_version == expected


async def test_changelog_feed_without_any_version_is_not_a_signal() -> None:
    url = "https://feeds.example.com/novers"
    source = _source(WatcherSourceKind.CHANGELOG_FEED, url)
    body = b'<?xml version="1.0"?><rss><channel><item><title>We are hiring</title></item></channel></rss>'
    snap = await ChangelogFeedAdapter().fetch(source, WatcherCursor.empty(source.key),
                                              FakeFetcher({url: FetchResponse(status=200, body=body)}), now=NOW)
    assert snap.latest_version is None and snap.notes


async def test_changelog_feed_refuses_dtd_and_entities() -> None:
    url = "https://feeds.example.com/evil"
    source = _source(WatcherSourceKind.CHANGELOG_FEED, url)
    evil = b'<?xml version="1.0"?><!DOCTYPE lolz [<!ENTITY a "aaaa">]><rss><channel><item><title>v1.0.0</title></item></channel></rss>'
    with pytest.raises(PermanentFetchError, match="DTD"):
        await ChangelogFeedAdapter().fetch(source, WatcherCursor.empty(source.key),
                                           FakeFetcher({url: FetchResponse(status=200, body=evil)}), now=NOW)


def test_http_status_mapping() -> None:
    raise_for_response(FetchResponse(status=200), what="x")
    raise_for_response(FetchResponse(status=304), what="x")
    with pytest.raises(RateLimitedError) as limited:
        raise_for_response(FetchResponse(status=429, retry_after_seconds=90.0), what="x")
    assert limited.value.retry_after_seconds == 90.0 and limited.value.retryable
    with pytest.raises(RateLimitedError):
        raise_for_response(FetchResponse(status=403, rate_limit_remaining=0), what="x")
    with pytest.raises(PermanentFetchError):
        raise_for_response(FetchResponse(status=403, rate_limit_remaining=10), what="x")
    for status in (500, 502, 503, 408):
        with pytest.raises(TransientFetchError):
            raise_for_response(FetchResponse(status=status), what="x")
    with pytest.raises(PermanentFetchError) as missing:
        raise_for_response(FetchResponse(status=404), what="x")
    assert not missing.value.retryable


def test_backoff_grows_is_capped_jittered_and_respects_retry_after() -> None:
    low, high = (lambda: 0.0), (lambda: 1.0)
    assert backoff_delay_seconds(1, base=30, jitter=low) == 15 and backoff_delay_seconds(1, base=30, jitter=high) == 30
    assert backoff_delay_seconds(3, base=30, jitter=high) == 120
    assert backoff_delay_seconds(30, base=30, cap=600, jitter=high) == 600
    assert backoff_delay_seconds(1, base=30, jitter=low, retry_after=300) == 300
    assert backoff_delay_seconds(1, base=30, cap=100, jitter=low, retry_after=5000) == 100
    with pytest.raises(ValueError):
        backoff_delay_seconds(0)


def test_source_keys_normalize_so_shared_fetches_dedupe() -> None:
    target = DependencyTarget(ecosystem=Ecosystem.PYPI)
    a = WatcherSource(WatcherSourceKind.PYPI, "Acme_SDK", target)
    b = WatcherSource(WatcherSourceKind.PYPI, "acme-sdk", target)
    assert a.key == b.key == "pypi:acme-sdk"
    assert WatcherSource(WatcherSourceKind.GITHUB_RELEASES, "Acme/SDK/", target).key == "github_releases:acme/sdk"
    assert WatcherSource(WatcherSourceKind.OPENAPI_URL, "https://a.example/s.json#frag", target).key.endswith("s.json")
