"""M11.1/M11.3/M11.5: snapshot diff, cursor/idempotency, duplicate suppression,
the watch registry, the launch gate and URL safety."""

from __future__ import annotations

import ast
import json
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import httpx
import pytest

from patchfrog.dependencies.domain import Ecosystem
from patchfrog.upstream.domain import ChangeRisk, DependencyTarget
from patchfrog.upstream.events import parse_contract_document
from patchfrog.watchers.adapters.manual import snapshot_from_contract, snapshot_from_version
from patchfrog.watchers.diff import detect_change
from patchfrog.watchers.domain import (
    MAX_EMITTED_FINGERPRINTS,
    WatcherCursor,
    WatcherSource,
    WatcherSourceKind,
    WatchOutcomeKind,
)
from patchfrog.watchers.fetch import (
    FakeFetcher,
    FetchResponse,
    HttpxFetcher,
    PermanentFetchError,
    UnsafeSourceError,
)
from patchfrog.watchers.gate import should_launch_campaign
from patchfrog.watchers.net import UnsafeUrlError, validate_public_url
from patchfrog.watchers.pipeline import ingest_snapshot, poll_source
from patchfrog.watchers.registry import (
    ConfiguredSource,
    DependencySubscription,
    required_watchers,
    subscribers_for,
)
from tests.support.campaigns import NOW

PYPI = WatcherSource(
    WatcherSourceKind.PYPI, "acme-sdk", DependencyTarget(ecosystem=Ecosystem.PYPI, package_name="acme-sdk")
)
K = WatchOutcomeKind


def _versions(*versions: str) -> FetchResponse:
    return FetchResponse(status=200, body=json.dumps(
        {"releases": {v: [{"upload_time_iso_8601": "2026-01-01T00:00:00Z"}] for v in versions}}
    ).encode())


async def _poll(fetcher: FakeFetcher, cursor: WatcherCursor | None, *, hours: int = 0):  # type: ignore[no-untyped-def]
    return await poll_source(PYPI, cursor, fetcher=fetcher, now=NOW + timedelta(hours=hours))


URL = "https://pypi.org/pypi/acme-sdk/json"


async def test_first_observation_is_a_baseline_and_never_replays_history() -> None:
    outcome = await _poll(FakeFetcher({URL: _versions("1.0.0", "1.4.0", "2.0.0")}), None)
    assert outcome.kind is K.BASELINE and outcome.change is None
    assert outcome.cursor.last_version == "2.0.0" and outcome.cursor.observations == 1


async def test_a_new_release_emits_one_version_change_then_is_idempotent() -> None:
    base = await _poll(FakeFetcher({URL: _versions("1.4.0")}), None)
    fetcher = FakeFetcher({URL: _versions("1.4.0", "2.0.0")})
    changed = await _poll(fetcher, base.cursor, hours=1)
    assert changed.kind is K.CHANGED and changed.change is not None
    event = changed.change.event
    assert (event.old.version, event.new.version) == ("1.4.0", "2.0.0")
    assert event.classification.risk is ChangeRisk.REVIEW_REQUIRED  # a version bump alone proves nothing structural
    assert changed.change.source_key == "pypi:acme-sdk" and changed.cursor.last_version == "2.0.0"
    again = await _poll(fetcher, changed.cursor, hours=2)
    assert again.kind is K.UNCHANGED and again.change is None  # same upstream state: no second event


async def test_patch_and_minor_bumps_are_low_risk() -> None:
    base = await _poll(FakeFetcher({URL: _versions("1.4.0")}), None)
    outcome = await _poll(FakeFetcher({URL: _versions("1.4.0", "1.4.1")}), base.cursor)
    assert outcome.change is not None and outcome.change.event.classification.risk in (ChangeRisk.SAFE, ChangeRisk.LOW_RISK)


async def test_duplicate_events_are_suppressed_even_if_the_cursor_regresses() -> None:
    base = await _poll(FakeFetcher({URL: _versions("1.4.0")}), None)
    fetcher = FakeFetcher({URL: _versions("1.4.0", "2.0.0")})
    first = await _poll(fetcher, base.cursor)
    assert first.kind is K.CHANGED
    # a worker restores an older cursor but remembers what it already emitted
    stale_cursor = replace(base.cursor, emitted=first.cursor.emitted)
    second = await _poll(fetcher, stale_cursor)
    assert second.kind is K.DUPLICATE_SUPPRESSED and second.change is None
    assert second.cursor.last_version == "2.0.0"


async def test_a_decreasing_latest_never_regresses_the_cursor() -> None:
    cursor = WatcherCursor(source_key=PYPI.key, last_version="2.0.0")
    outcome = await _poll(FakeFetcher({URL: _versions("1.9.0")}), cursor)
    assert outcome.kind is K.UNCHANGED and outcome.cursor.last_version == "2.0.0"


async def test_unparseable_versions_are_skipped_not_guessed() -> None:
    outcome = detect_change(PYPI, snapshot_from_version(PYPI, "nightly-build", now=NOW),
                            WatcherCursor(source_key=PYPI.key, last_version="1.0.0"))
    assert outcome.kind is K.SKIPPED and outcome.change is None


async def test_not_modified_keeps_the_cursor_and_counts_the_observation() -> None:
    cursor = WatcherCursor(source_key=PYPI.key, last_version="1.0.0", etag='"e"')
    outcome = await _poll(FakeFetcher({URL: FetchResponse(status=304)}), cursor)
    assert outcome.kind is K.UNCHANGED and outcome.cursor.observations == 1 and outcome.cursor.last_version == "1.0.0"


def test_emitted_fingerprints_are_bounded() -> None:
    cursor = WatcherCursor(source_key="k", last_version="1.0.0")
    for i in range(MAX_EMITTED_FINGERPRINTS + 20):
        snap = snapshot_from_version(PYPI, f"1.{i + 1}.0", now=NOW)
        cursor = detect_change(PYPI, snap, replace(cursor, last_version=f"1.{i}.0")).cursor
    assert len(cursor.emitted) == MAX_EMITTED_FINGERPRINTS


def test_cursor_round_trips_through_json_and_rejects_unknown_formats() -> None:
    cursor = WatcherCursor(
        source_key="pypi:x", last_version="1.0.0", etag='"e"', emitted=("a", "b"), observations=3, last_observed_at=NOW,
        last_contract_fingerprint="fp", last_contract_json="{}", last_contract_ref="r", last_contract_version="1",
    )
    assert WatcherCursor.from_json(cursor.to_json()) == cursor
    with pytest.raises(ValueError, match="unsupported"):
        WatcherCursor.from_json(json.dumps({"v": 99, "source_key": "x"}))


def _openapi(extra_param: bool) -> dict[str, object]:
    params: list[dict[str, object]] = [{"name": "limit", "in": "query", "required": False, "schema": {"type": "integer"}}]
    if extra_param:
        params.append({"name": "tenant", "in": "query", "required": True, "schema": {"type": "string"}})
    return {"openapi": "3.0.0", "info": {"title": "T", "version": "2.0.0" if extra_param else "1.0.0"},
            "paths": {"/v1/items": {"get": {"parameters": params, "responses": {"200": {"description": "ok"}}}}}}


def test_openapi_revision_becomes_a_breaking_structural_event() -> None:
    source = WatcherSource(WatcherSourceKind.OPENAPI_URL, "https://api.example.com/openapi.json",
                           DependencyTarget(api_hosts=("api.example.com",)))
    old = parse_contract_document(_openapi(False), ref="spec")
    new = parse_contract_document(_openapi(True), ref="spec")
    base = ingest_snapshot(source, snapshot_from_contract(source, old, now=NOW), None)
    assert base.kind is K.BASELINE and base.cursor.last_contract_json
    same = ingest_snapshot(source, snapshot_from_contract(source, old, now=NOW), base.cursor)
    assert same.kind is K.UNCHANGED
    changed = ingest_snapshot(source, snapshot_from_contract(source, new, now=NOW), base.cursor)
    assert changed.kind is K.CHANGED and changed.change is not None
    event = changed.change.event
    assert event.classification.risk is ChangeRisk.BREAKING and event.classification.has_structural_evidence
    assert any(i.kind.value == "parameter_added_required" for i in event.diff)
    assert changed.cursor.last_contract_fingerprint == new.fingerprint
    # polling the same new contract again emits nothing
    assert ingest_snapshot(source, snapshot_from_contract(source, new, now=NOW), changed.cursor).kind is K.UNCHANGED
    # and the same change observed from a restored cursor is suppressed, not duplicated
    replay = ingest_snapshot(source, snapshot_from_contract(source, new, now=NOW),
                             replace(base.cursor, emitted=changed.cursor.emitted))
    assert replay.kind is K.DUPLICATE_SUPPRESSED


def test_a_contract_too_large_to_remember_cannot_be_diffed_and_says_so() -> None:
    source = WatcherSource(WatcherSourceKind.MANUAL, "m", DependencyTarget(api_hosts=("a.example.com",)))
    new = parse_contract_document(_openapi(True), ref="spec")
    cursor = WatcherCursor(source_key=source.key, last_contract_fingerprint="old", last_contract_json=None)
    outcome = ingest_snapshot(source, snapshot_from_contract(source, new, now=NOW), cursor)
    assert outcome.kind is K.SKIPPED and "too large" in outcome.notes[0]
    assert outcome.cursor.last_contract_fingerprint == new.fingerprint  # the baseline moves forward


def test_mismatched_contract_formats_are_skipped_not_crashed() -> None:
    source = WatcherSource(WatcherSourceKind.MANUAL, "m", DependencyTarget(api_hosts=("a.example.com",)))
    surface = parse_contract_document(
        {"patchfrog_sdk_surface": 1, "package": "p", "ecosystem": "pypi", "version": "1", "modules": ["p"],
         "symbols": {"Client": {}}}, ref="s")
    base = ingest_snapshot(source, snapshot_from_contract(source, surface, now=NOW), None)
    outcome = ingest_snapshot(source, snapshot_from_contract(source, parse_contract_document(_openapi(True), ref="o"), now=NOW),
                              base.cursor)
    assert outcome.kind is K.SKIPPED and outcome.change is None


async def test_polling_a_manual_source_is_an_error_not_a_silent_noop() -> None:
    manual = WatcherSource(WatcherSourceKind.MANUAL, "m", DependencyTarget())
    with pytest.raises(PermanentFetchError, match="submitted, not polled"):
        await poll_source(manual, None, fetcher=FakeFetcher(), now=NOW)


# -- watch registry (M11.1) ---------------------------------------------------------------


def _sub(workspace: str, package: str | None, *, ecosystem: str = "pypi", kind: str = "sdk") -> DependencySubscription:
    return DependencySubscription(workspace, f"{workspace}/repo", kind, ecosystem, package)


def test_fifty_workspaces_using_one_package_cause_one_fetch() -> None:
    subs = [_sub(f"ws-{i:02d}", "OpenAI_SDK") for i in range(50)]
    requirements = required_watchers(subs)
    assert len(requirements) == 1
    assert requirements[0].source.key == "pypi:openai-sdk" and len(requirements[0].subscribers) == 50
    assert requirements[0].origin == "dependency"
    assert subscribers_for(requirements, "pypi:openai-sdk") == requirements[0].subscribers
    assert subscribers_for(requirements, "pypi:other") == ()


def test_only_dependencies_actually_used_are_watched() -> None:
    subs = [
        _sub("a", "requests"), _sub("a", "left-pad", ecosystem="npm"), _sub("a", None),
        _sub("a", "somegem", ecosystem="rubygems"), _sub("a", "stripe-api", kind="http_api"),
        _sub("a", "openapi-doc", kind="openapi_contract", ecosystem="openapi"),
    ]
    keys = [r.source.key for r in required_watchers(subs)]
    assert keys == ["npm:left-pad", "pypi:requests"]


def test_configured_sources_are_deduplicated_and_win_over_derived_targets() -> None:
    specific = WatcherSource(WatcherSourceKind.PYPI, "requests",
                             DependencyTarget(ecosystem=Ecosystem.PYPI, package_name="requests", provider_key="http"))
    gh = WatcherSource(WatcherSourceKind.GITHUB_RELEASES, "psf/requests", DependencyTarget(package_name="requests"))
    requirements = required_watchers(
        [_sub("a", "requests"), _sub("b", "requests")],
        [ConfiguredSource("c", specific), ConfiguredSource("a", gh), ConfiguredSource("b", gh)],
    )
    by_key = {r.source.key: r for r in requirements}
    assert set(by_key) == {"pypi:requests", "github_releases:psf/requests"}
    assert by_key["pypi:requests"].subscribers == ("a", "b", "c") and by_key["pypi:requests"].origin == "configured"
    assert by_key["pypi:requests"].source.target.provider_key == "http"
    assert by_key["github_releases:psf/requests"].subscribers == ("a", "b")


# -- gate (M11.5/M11.11) -------------------------------------------------------------------


async def test_the_launch_gate_requires_structural_proof_by_default() -> None:
    base = await _poll(FakeFetcher({URL: _versions("1.4.0")}), None)
    major = await _poll(FakeFetcher({URL: _versions("1.4.0", "2.0.0")}), base.cursor)
    assert major.change is not None
    decision = should_launch_campaign(major.change.event)
    assert not decision.launch and "recorded, no campaign opened" in decision.reason
    assert should_launch_campaign(major.change.event, min_risk=ChangeRisk.REVIEW_REQUIRED).launch


# -- URL safety (M11.2) ----------------------------------------------------------------------


async def _resolve_public(host: str, port: int) -> list[str]:
    return ["93.184.216.34"]


async def _resolve_private(host: str, port: int) -> list[str]:
    return ["10.0.0.5"]


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com/x", "https://user:pw@example.com/x", "https://localhost/x", "https://127.0.0.1/x",
        "https://169.254.169.254/latest/meta-data", "https://[::1]/x", "https://10.1.2.3/x", "https://service.internal/x",
        "https://example.com:6379/x", "ftp://example.com/x", "https:///nohost", "https://192.168.0.1/x",
    ],
)
async def test_unsafe_urls_are_refused(url: str) -> None:
    with pytest.raises(UnsafeUrlError):
        await validate_public_url(url, resolver=_resolve_public)


async def test_hostnames_resolving_to_private_addresses_are_refused() -> None:
    with pytest.raises(UnsafeUrlError, match="non-public"):
        await validate_public_url("https://sneaky.example.com/x", resolver=_resolve_private)
    assert await validate_public_url("https://example.com/x", resolver=_resolve_public) == "https://example.com/x"


async def test_httpx_fetcher_refuses_internal_targets_and_unsafe_redirects() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "good.example.com":
            return httpx.Response(302, headers={"location": "https://169.254.169.254/latest"})
        return httpx.Response(200, content=b"{}")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False)
    fetcher = HttpxFetcher(client=client, resolver=_resolve_public)
    with pytest.raises(UnsafeSourceError):
        await fetcher.get("https://127.0.0.1/x")
    with pytest.raises(UnsafeSourceError):
        await fetcher.get("https://good.example.com/x")  # redirect hop to a metadata address
    assert (await fetcher.get("https://other.example.com/x")).status == 200


async def test_httpx_fetcher_caps_size_and_scopes_auth_headers_to_their_host() -> None:
    seen: dict[str, str | None] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen[request.url.host] = request.headers.get("authorization")
        if request.url.path == "/big":
            return httpx.Response(200, content=b"x" * 2000)
        return httpx.Response(200, content=b"{}", headers={"etag": '"v1"', "x-ratelimit-remaining": "7"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False)
    fetcher = HttpxFetcher(client=client, resolver=_resolve_public,
                           auth_headers={"api.github.com": {"Authorization": "Bearer test-token-value"}})
    response = await fetcher.get("https://api.github.com/x")
    assert response.etag == '"v1"' and response.rate_limit_remaining == 7
    await fetcher.get("https://example.com/x")
    assert seen["api.github.com"] == "Bearer test-token-value" and seen["example.com"] is None
    with pytest.raises(PermanentFetchError, match="larger"):
        await fetcher.get("https://example.com/big", max_bytes=100)


# -- structural invariants -----------------------------------------------------------------

_WATCHERS = Path(__file__).resolve().parents[2] / "patchfrog" / "watchers"


def test_watchers_never_import_a_provider_or_the_review_engine() -> None:
    for path in _WATCHERS.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.ImportFrom) and node.module:
                assert not node.module.startswith(
                    ("patchfrog.review", "patchfrog.routing", "anthropic", "openai", "google", "patchfrog.persistence")
                ), f"{path.name} imports {node.module}"
            if isinstance(node, ast.Import):
                assert not any(a.name.split(".")[0] in ("anthropic", "openai", "google") for a in node.names)


def test_watchers_hold_no_workspace_or_cloud_concepts() -> None:
    for path in _WATCHERS.rglob("*.py"):
        text = path.read_text().lower()
        assert "patchfrog_cloud" not in text and "celery" not in text, path.name
