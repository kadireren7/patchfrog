"""M11: an event survives a process boundary unchanged, and so does everything computed from it."""

from __future__ import annotations

import json

import pytest

from patchfrog.upstream.domain import DependencyTarget
from patchfrog.upstream.events import (
    build_contract_change,
    build_version_change,
    load_contract_file,
    parse_contract_document,
)
from patchfrog.upstream.serialize import (
    MAX_SERIALIZED_CONTRACT_BYTES,
    event_from_json,
    event_to_json,
)
from tests.support.campaigns import ACME, INTERNAL, acme_event


def test_contract_event_round_trips_exactly() -> None:
    event, _ = acme_event()
    restored = event_from_json(event_to_json(event))
    assert restored == event
    assert restored.fingerprint == event.fingerprint and restored.old.normalized == event.old.normalized


def test_version_event_and_release_round_trip() -> None:
    from patchfrog.upstream.domain import DependencyRelease

    release = DependencyRelease(version="2.0.0", tag="v2.0.0", published_at="2026-01-01", url="https://e.com/r",
                                notes_sha256="ab" * 32, prerelease=False)
    event = build_version_change(DependencyTarget(package_name="acme-ai"), "1.4.0", "2.0.0", release=release)
    assert event_from_json(event_to_json(event)) == event


def test_internal_surface_event_round_trips() -> None:
    event = build_contract_change(
        load_contract_file(INTERNAL / "contract" / "old.yaml"), load_contract_file(INTERNAL / "contract" / "new.yaml")
    )
    assert event_from_json(event_to_json(event)) == event


def test_serialization_is_deterministic() -> None:
    event, _ = acme_event()
    assert event_to_json(event) == event_to_json(event_from_json(event_to_json(event)))


def test_unknown_serialization_version_is_rejected() -> None:
    event, _ = acme_event()
    data = json.loads(event_to_json(event))
    data["v"] = 99
    with pytest.raises(ValueError, match="unsupported"):
        event_from_json(json.dumps(data))


def test_oversized_contract_is_dropped_loudly_not_silently() -> None:
    doc = {"openapi": "3.0.0", "info": {"title": "T", "version": "1"},
           "paths": {f"/v1/resource-{i}/items": {"get": {"parameters": [{"name": f"q{i}", "in": "query"}],
                                                          "responses": {"200": {"description": "ok"}}}}
                     for i in range(5000)}}
    old = parse_contract_document(doc, ref="o")
    new = parse_contract_document({**doc, "info": {"title": "T", "version": "2"}, "paths": {}}, ref="n")
    event = build_contract_change(old, new)
    text = event_to_json(event)
    assert len(json.dumps(dict(event.old.normalized or {}), sort_keys=True, separators=(",", ":"))) \
        > MAX_SERIALIZED_CONTRACT_BYTES
    restored = event_from_json(text)
    assert restored.old.normalized is None
    assert any("too large to carry" in n for n in restored.notes)


def test_a_restored_event_yields_the_same_campaign_inputs() -> None:
    from patchfrog.upstream.workspace import analyze_repository

    event, hints = acme_event()
    restored = event_from_json(event_to_json(event))
    for name in ("acme-web", "acme-worker", "acme-search"):
        a, _ = analyze_repository(ACME / "repos" / name, event, hints=hints, repository=name)
        b, _ = analyze_repository(ACME / "repos" / name, restored, hints=hints, repository=name)
        assert (a.status, a.direct_count, a.potential_count) == (b.status, b.direct_count, b.potential_count)


def test_parse_contract_text_matches_loading_the_same_file() -> None:
    from patchfrog.upstream.events import ContractLoadError, parse_contract_text

    path = ACME / "contract" / "new.yaml"
    from_file = load_contract_file(path)
    from_text = parse_contract_text(path.read_text(), ref="new")
    assert from_text.fingerprint == from_file.fingerprint and from_text.format == "sdk_surface"
    for bad in ("not: [a contract", "just text", "{}"):
        with pytest.raises(ContractLoadError):
            parse_contract_text(bad, ref="x")
    with pytest.raises(ContractLoadError, match="larger"):
        parse_contract_text("a" * 6_000_000, ref="x")
