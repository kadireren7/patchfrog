"""M6.2-M6.4: deterministic contract diff, SDK/package change model,
classifier and change hints."""

from __future__ import annotations

import copy
from typing import Any

import pytest

from patchfrog.dependencies.domain import Ecosystem
from patchfrog.upstream.classify import classify
from patchfrog.upstream.domain import (
    ChangeRisk,
    CompatibilityClass,
    DependencyTarget,
    DiffItemKind,
    ExternalChangeKind,
    ExternalChangeSource,
    SubjectKind,
)
from patchfrog.upstream.events import (
    ContractLoadError,
    build_contract_change,
    build_version_change,
    parse_contract_document,
    release_from_metadata,
)
from patchfrog.upstream.hints import HintError, parse_hints
from patchfrog.upstream.package_version import parse_version, version_diff_items
from patchfrog.upstream.sdk_surface import SurfaceError, parse_surface

BR = CompatibilityClass.BREAKING
PB = CompatibilityClass.POTENTIALLY_BREAKING
NB = CompatibilityClass.NON_BREAKING
UNK = CompatibilityClass.UNKNOWN


def _spec() -> dict[str, Any]:
    return {
        "openapi": "3.0.3",
        "info": {"title": "Payments", "version": "1.0.0"},
        "servers": [{"url": "https://api.payments.example/v1"}],
        "components": {
            "securitySchemes": {"apiKey": {"type": "apiKey", "in": "header", "name": "X-Api-Key"}},
            "schemas": {
                "Charge": {
                    "type": "object",
                    "required": ["id", "amount"],
                    "properties": {
                        "id": {"type": "string"},
                        "amount": {"type": "integer"},
                        "status": {"type": "string", "enum": ["pending", "paid"]},
                        "note": {"type": "string"},
                    },
                },
                "NewCharge": {
                    "type": "object",
                    "required": ["amount"],
                    "properties": {
                        "amount": {"type": "integer"},
                        "currency": {"type": "string", "enum": ["usd", "eur", "gbp"]},
                        "memo": {"type": "string"},
                    },
                },
            },
        },
        "security": [{"apiKey": []}],
        "paths": {
            "/charges": {
                "get": {
                    "operationId": "listCharges",
                    "parameters": [
                        {"name": "limit", "in": "query", "schema": {"type": "integer"}},
                        {"name": "cursor", "in": "query", "schema": {"type": "string"}},
                    ],
                    "responses": {
                        "200": {"description": "ok", "content": {"application/json": {"schema": {
                            "type": "array", "items": {"$ref": "#/components/schemas/Charge"}}}}},
                        "404": {"description": "missing"},
                    },
                },
                "post": {
                    "operationId": "createCharge",
                    "requestBody": {"required": True, "content": {"application/json": {
                        "schema": {"$ref": "#/components/schemas/NewCharge"}}}},
                    "responses": {"201": {"description": "created", "content": {"application/json": {
                        "schema": {"$ref": "#/components/schemas/Charge"}}}}},
                },
            },
            "/charges/{id}/refund": {
                "post": {
                    "operationId": "refundCharge",
                    "parameters": [{"name": "id", "in": "path", "required": True, "schema": {"type": "string"}}],
                    "responses": {"200": {"description": "ok"}},
                }
            },
            "/reports/{id}": {
                "get": {
                    "parameters": [{"name": "id", "in": "path", "required": True, "schema": {"type": "string"}}],
                    "responses": {"200": {"description": "ok"}},
                }
            },
        },
    }


def _diff(old: dict[str, Any], new: dict[str, Any], hints: dict[str, Any] | None = None) -> Any:
    return build_contract_change(
        parse_contract_document(old, ref="old.yaml"),
        parse_contract_document(new, ref="new.yaml"),
        hints=parse_hints(hints),
    )


def _by_kind(event: Any, kind: DiffItemKind) -> list[Any]:
    return [i for i in event.diff if i.kind is kind]


# -- endpoints -------------------------------------------------------------


def test_identical_contracts_produce_no_diff_and_are_safe() -> None:
    event = _diff(_spec(), _spec())
    assert event.diff == ()
    assert event.classification.risk is ChangeRisk.SAFE
    assert event.kind is ExternalChangeKind.CONTRACT_REVISION


def test_yaml_key_order_and_placeholder_names_do_not_create_diffs() -> None:
    new = _spec()
    new["paths"]["/charges/{chargeId}/refund"] = new["paths"].pop("/charges/{id}/refund")
    new["paths"] = dict(reversed(list(new["paths"].items())))
    new["paths"]["/charges/{chargeId}/refund"]["post"]["parameters"][0]["name"] = "chargeId"
    assert _diff(_spec(), new).diff == ()


def test_endpoint_added_is_non_breaking_and_removed_is_breaking() -> None:
    new = _spec()
    new["paths"]["/payouts"] = {"get": {"responses": {"200": {"description": "ok"}}}}
    del new["paths"]["/reports/{id}"]
    event = _diff(_spec(), new)
    (added,) = _by_kind(event, DiffItemKind.ENDPOINT_ADDED)
    (removed,) = _by_kind(event, DiffItemKind.ENDPOINT_REMOVED)
    assert added.compatibility is NB and removed.compatibility is BR
    assert removed.subject.kind is SubjectKind.OPERATION
    assert (removed.subject.method, removed.subject.path) == ("get", "/reports/{id}")
    assert removed.replacement is None
    assert event.classification.risk is ChangeRisk.BREAKING


def test_method_removed_on_a_surviving_path_is_operation_removed() -> None:
    new = _spec()
    del new["paths"]["/charges"]["post"]
    new["paths"]["/charges"]["put"] = {"responses": {"200": {"description": "ok"}}}
    event = _diff(_spec(), new)
    assert [i.subject.method for i in _by_kind(event, DiffItemKind.OPERATION_REMOVED)] == ["post"]
    assert [i.subject.method for i in _by_kind(event, DiffItemKind.OPERATION_ADDED)] == ["put"]


def test_path_change_is_recognised_only_by_operation_id_or_hint() -> None:
    new = _spec()
    new["paths"]["/charges/{id}/refunds"] = new["paths"].pop("/charges/{id}/refund")
    event = _diff(_spec(), new)
    (moved,) = _by_kind(event, DiffItemKind.ENDPOINT_PATH_CHANGED)
    assert moved.replacement == "POST /charges/{id}/refunds"
    assert "replacement.via=operation_id" in moved.evidence
    assert not _by_kind(event, DiffItemKind.ENDPOINT_REMOVED)

    # Without an operationId, a path change is a removal + an addition...
    old = _spec()
    new = _spec()
    new["paths"]["/v2/reports/{id}"] = new["paths"].pop("/reports/{id}")
    event = _diff(old, new)
    assert _by_kind(event, DiffItemKind.ENDPOINT_REMOVED) and _by_kind(event, DiffItemKind.ENDPOINT_ADDED)
    assert not _by_kind(event, DiffItemKind.ENDPOINT_PATH_CHANGED)
    # ... unless an explicit hint says otherwise.
    event = _diff(old, new, {"patchfrog_change_hints": 1, "endpoint_replacements": [
        {"old": "GET /reports/{id}", "new": "GET /v2/reports/{id}"}]})
    (moved,) = _by_kind(event, DiffItemKind.ENDPOINT_PATH_CHANGED)
    assert moved.replacement == "GET /v2/reports/{id}" and "replacement.via=hint" in moved.evidence
    assert not _by_kind(event, DiffItemKind.ENDPOINT_ADDED)


def test_hint_pointing_at_a_missing_endpoint_is_ignored_with_a_note() -> None:
    new = _spec()
    del new["paths"]["/reports/{id}"]
    event = _diff(_spec(), new, {"patchfrog_change_hints": 1, "endpoint_replacements": [
        {"old": "GET /reports/{id}", "new": "GET /nowhere"}]})
    assert _by_kind(event, DiffItemKind.ENDPOINT_REMOVED)
    assert any("not in the new contract" in n for n in event.notes)


def test_deprecation_is_non_breaking() -> None:
    new = _spec()
    new["paths"]["/reports/{id}"]["get"]["deprecated"] = True
    event = _diff(_spec(), new)
    (item,) = event.diff
    assert item.kind is DiffItemKind.OPERATION_DEPRECATED and item.compatibility is NB
    assert event.classification.risk is ChangeRisk.LOW_RISK


# -- parameters -------------------------------------------------------------


def test_parameter_changes() -> None:
    new = _spec()
    params = new["paths"]["/charges"]["get"]["parameters"]
    params[0]["required"] = True  # limit optional -> required
    params.pop(1)  # cursor removed
    params.append({"name": "expand", "in": "query", "schema": {"type": "string"}})
    params.append({"name": "account", "in": "header", "required": True, "schema": {"type": "string"}})
    event = _diff(_spec(), new)
    assert _by_kind(event, DiffItemKind.PARAMETER_BECAME_REQUIRED)[0].compatibility is BR
    assert _by_kind(event, DiffItemKind.PARAMETER_REMOVED)[0].compatibility is PB
    assert _by_kind(event, DiffItemKind.PARAMETER_ADDED_OPTIONAL)[0].compatibility is NB
    required = _by_kind(event, DiffItemKind.PARAMETER_ADDED_REQUIRED)[0]
    assert required.compatibility is BR and required.subject.member == "account"


def test_parameter_type_and_enum_changes_are_request_side() -> None:
    old = _spec()
    old["paths"]["/charges"]["get"]["parameters"].append(
        {"name": "status", "in": "query", "schema": {"type": "string", "enum": ["a", "b", "c"]}})
    new = copy.deepcopy(old)
    new["paths"]["/charges"]["get"]["parameters"][0]["schema"] = {"type": "string"}
    new["paths"]["/charges"]["get"]["parameters"][2]["schema"]["enum"] = ["a", "b", "d"]
    event = _diff(old, new)
    assert _by_kind(event, DiffItemKind.PARAMETER_TYPE_CHANGED)[0].compatibility is BR
    assert _by_kind(event, DiffItemKind.PARAMETER_ENUM_NARROWED)[0].compatibility is BR
    assert _by_kind(event, DiffItemKind.PARAMETER_ENUM_EXPANDED)[0].compatibility is NB


def test_integer_to_number_widening_is_direction_aware() -> None:
    old = _spec()
    new = copy.deepcopy(old)
    new["paths"]["/charges"]["get"]["parameters"][0]["schema"]["type"] = "number"  # request: widen
    new["components"]["schemas"]["Charge"]["properties"]["amount"]["type"] = "number"  # response: widen
    event = _diff(old, new)
    (param,) = _by_kind(event, DiffItemKind.PARAMETER_TYPE_CHANGED)
    assert param.compatibility is NB
    (schema,) = _by_kind(event, DiffItemKind.SCHEMA_TYPE_CHANGED)
    assert schema.compatibility is BR  # Charge is response-only


def test_hinted_parameter_rename() -> None:
    new = _spec()
    new["paths"]["/charges"]["get"]["parameters"][1]["name"] = "starting_after"
    hints = {"patchfrog_change_hints": 1, "operation_parameter_renames": [
        {"operation": "GET /charges", "in": "query", "old": "cursor", "new": "starting_after"}]}
    event = _diff(_spec(), new, hints)
    (rename,) = _by_kind(event, DiffItemKind.PARAMETER_RENAMED)
    assert rename.replacement == "starting_after" and rename.subject.member == "cursor"
    assert not _by_kind(event, DiffItemKind.PARAMETER_REMOVED)
    assert not _by_kind(event, DiffItemKind.PARAMETER_ADDED_OPTIONAL)


# -- request bodies ---------------------------------------------------------


def test_request_body_changes() -> None:
    new = _spec()
    new["paths"]["/charges/{id}/refund"]["post"]["requestBody"] = {
        "required": True, "content": {"application/json": {"schema": {"type": "object"}}}}
    new["components"]["schemas"]["NewCharge"]["required"] = ["amount", "currency"]
    new["components"]["schemas"]["NewCharge"]["properties"]["source"] = {"type": "string"}
    new["components"]["schemas"]["NewCharge"]["properties"]["customer"] = {"type": "string"}
    new["components"]["schemas"]["NewCharge"]["required"].append("customer")
    del new["components"]["schemas"]["NewCharge"]["properties"]["memo"]
    new["components"]["schemas"]["NewCharge"]["properties"]["currency"]["enum"] = ["usd", "eur"]
    event = _diff(_spec(), new)
    assert _by_kind(event, DiffItemKind.REQUEST_BODY_ADDED_REQUIRED)[0].compatibility is BR
    kinds = {(i.kind, i.subject.member): i.compatibility for i in event.diff if i.subject.name == "NewCharge"}
    # NewCharge is request-only: requiredness and narrowing break, removal is only potential.
    assert kinds[(DiffItemKind.SCHEMA_PROPERTY_BECAME_REQUIRED, "currency")] is BR
    assert kinds[(DiffItemKind.SCHEMA_PROPERTY_ADDED_REQUIRED, "customer")] is BR
    assert kinds[(DiffItemKind.SCHEMA_PROPERTY_ADDED_OPTIONAL, "source")] is NB
    assert kinds[(DiffItemKind.SCHEMA_PROPERTY_REMOVED, "memo")] is PB
    assert kinds[(DiffItemKind.SCHEMA_ENUM_NARROWED, "currency")] is BR


def test_inline_request_body_field_rename_via_hint() -> None:
    old = _spec()
    old["paths"]["/charges/{id}/refund"]["post"]["requestBody"] = {"required": True, "content": {
        "application/json": {"schema": {"type": "object", "properties": {"reason": {"type": "string"}}}}}}
    new = copy.deepcopy(old)
    body = new["paths"]["/charges/{id}/refund"]["post"]["requestBody"]["content"]["application/json"]["schema"]
    body["properties"] = {"refund_reason": {"type": "string"}}
    event = _diff(old, new, {"patchfrog_change_hints": 1, "operation_parameter_renames": [
        {"operation": "POST /charges/{id}/refund", "in": "body", "old": "reason", "new": "refund_reason"}]})
    (rename,) = _by_kind(event, DiffItemKind.REQUEST_FIELD_RENAMED)
    assert rename.replacement == "refund_reason" and rename.compatibility is BR
    assert not _by_kind(event, DiffItemKind.REQUEST_FIELD_REMOVED)


# -- responses ---------------------------------------------------------------


def test_response_changes_are_consumer_side() -> None:
    new = _spec()
    del new["paths"]["/charges"]["get"]["responses"]["404"]
    del new["paths"]["/charges/{id}/refund"]["post"]["responses"]["200"]
    new["paths"]["/charges/{id}/refund"]["post"]["responses"]["202"] = {"description": "accepted"}
    charge = new["components"]["schemas"]["Charge"]
    del charge["properties"]["note"]
    charge["required"] = ["id"]
    charge["properties"]["status"]["enum"] = ["pending", "paid", "disputed"]
    charge["properties"]["fee"] = {"type": "integer"}
    event = _diff(_spec(), new)
    statuses = {i.subject.path: i.compatibility for i in _by_kind(event, DiffItemKind.RESPONSE_STATUS_REMOVED)}
    assert statuses == {"/charges": PB, "/charges/{id}/refund": BR}  # 404 vs 200
    kinds = {(i.kind, i.subject.member): i.compatibility for i in event.diff if i.subject.name == "Charge"}
    assert kinds[(DiffItemKind.SCHEMA_PROPERTY_REMOVED, "note")] is BR
    assert kinds[(DiffItemKind.SCHEMA_PROPERTY_BECAME_OPTIONAL, "amount")] is PB
    assert kinds[(DiffItemKind.SCHEMA_ENUM_EXPANDED, "status")] is PB
    assert kinds[(DiffItemKind.SCHEMA_PROPERTY_ADDED_OPTIONAL, "fee")] is NB


def test_inline_response_schema_uses_response_kinds() -> None:
    old = _spec()
    old["paths"]["/reports/{id}"]["get"]["responses"]["200"]["content"] = {"application/json": {"schema": {
        "type": "object", "properties": {"rows": {"type": "integer"}, "total": {"type": "integer"}}}}}
    new = copy.deepcopy(old)
    props = new["paths"]["/reports/{id}"]["get"]["responses"]["200"]["content"]["application/json"]["schema"]
    del props["properties"]["total"]
    props["properties"]["rows"]["type"] = "string"
    event = _diff(old, new)
    assert _by_kind(event, DiffItemKind.RESPONSE_FIELD_REMOVED)[0].compatibility is BR
    assert _by_kind(event, DiffItemKind.RESPONSE_TYPE_CHANGED)[0].compatibility is BR


def test_composed_schema_change_is_unknown_not_guessed() -> None:
    old = _spec()
    old["components"]["schemas"]["Charge"]["oneOf"] = [{"type": "object"}]
    new = copy.deepcopy(old)
    new["components"]["schemas"]["Charge"]["oneOf"] = [{"type": "object"}, {"type": "string"}]
    event = _diff(old, new)
    (item,) = event.diff
    assert item.compatibility is UNK and item.kind is DiffItemKind.SCHEMA_CHANGED
    assert event.classification.risk is ChangeRisk.REVIEW_REQUIRED


# -- auth ----------------------------------------------------------------------


def test_auth_requirement_added_on_an_operation() -> None:
    old = _spec()
    old["paths"]["/reports/{id}"]["get"]["security"] = []
    new = copy.deepcopy(old)
    new["paths"]["/reports/{id}"]["get"]["security"] = [{"apiKey": []}]
    event = _diff(old, new)
    (item,) = _by_kind(event, DiffItemKind.AUTH_REQUIREMENT_ADDED)
    assert item.compatibility is BR and item.subject.path == "/reports/{id}"


def test_global_auth_scheme_change_and_scope_change() -> None:
    new = _spec()
    new["components"]["securitySchemes"]["oauth"] = {"type": "oauth2"}
    new["security"] = [{"oauth": ["charges:read"]}]
    event = _diff(_spec(), new)
    (scheme,) = _by_kind(event, DiffItemKind.AUTH_SCHEME_CHANGED)
    assert scheme.subject.kind is SubjectKind.AUTH_GLOBAL and scheme.compatibility is BR

    old = _spec()
    old["security"] = [{"apiKey": ["read"]}]
    new = copy.deepcopy(old)
    new["security"] = [{"apiKey": ["read", "write"]}]
    (scope,) = _by_kind(_diff(old, new), DiffItemKind.AUTH_SCOPE_ADDED)
    assert "scope.added=apiKey:write" in scope.evidence
    # Adding an alternative credential set never breaks existing callers.
    new = copy.deepcopy(old)
    new["security"] = [{"apiKey": ["read"]}, {"oauth": []}]
    assert _diff(old, new).classification.risk in (ChangeRisk.SAFE, ChangeRisk.LOW_RISK)


def test_security_scheme_changes() -> None:
    new = _spec()
    new["components"]["securitySchemes"]["apiKey"]["name"] = "Authorization"
    event = _diff(_spec(), new)
    (item,) = _by_kind(event, DiffItemKind.SECURITY_SCHEME_CHANGED)
    assert item.compatibility is BR and "scheme.changed=name" in item.evidence


# -- SDK surfaces & versions ---------------------------------------------------


def _surface(version: str, symbols: dict[str, Any], modules: list[str] | None = None) -> dict[str, Any]:
    return {"patchfrog_sdk_surface": 1, "package": "acme-ai", "ecosystem": "pypi", "version": version,
            "modules": modules or ["acme_ai"], "symbols": symbols}


def test_sdk_surface_rename_with_hints_and_without() -> None:
    old = _surface("1.4.0", {
        "chat.create": {"params": {"prompt": {"required": True}, "mode": {"enum": ["fast", "slow"]}},
                        "returns": {"fields": {"text": "string"}}},
        "embeddings.create": {"params": {"input": {"required": True}}},
    }, ["acme_ai", "acme_ai.legacy"])
    new = _surface("2.0.0", {
        "responses.create": {"params": {"input": {"required": True}, "model": {"required": True},
                                        "mode": {"enum": ["speed", "slow"]}},
                             "returns": {"fields": {"output_text": "string"}}},
        "embeddings.create": {"params": {"input": {"required": True}}},
    })
    hints = {"patchfrog_change_hints": 1,
             "symbol_renames": [{"old": "chat.create", "new": "responses.create"}],
             "parameter_renames": [{"symbol": "chat.create", "old": "prompt", "new": "input"}],
             "enum_replacements": [{"symbol": "chat.create", "parameter": "mode", "old": "fast", "new": "speed"}],
             "return_field_renames": [{"symbol": "chat.create", "old": "text", "new": "output_text"}],
             "module_moves": [{"old": "acme_ai.legacy", "new": "acme_ai"}]}
    event = _diff(old, new, hints)
    kinds = {i.kind: i for i in event.diff}
    assert kinds[DiffItemKind.SDK_SYMBOL_RENAMED].replacement == "responses.create"
    assert kinds[DiffItemKind.SDK_PARAMETER_RENAMED].replacement == "input"
    assert kinds[DiffItemKind.SDK_PARAMETER_RENAMED].subject.name == "chat.create"  # consumer-visible old name
    assert kinds[DiffItemKind.SDK_PARAMETER_ADDED_REQUIRED].subject.member == "model"
    assert kinds[DiffItemKind.SDK_ENUM_VALUE_REPLACED].replacement == "speed"
    assert kinds[DiffItemKind.SDK_RETURN_FIELD_RENAMED].replacement == "output_text"
    assert kinds[DiffItemKind.SDK_MODULE_MOVED].replacement == "acme_ai"
    assert kinds[DiffItemKind.PACKAGE_MAJOR_BUMP].compatibility is PB
    assert DiffItemKind.SDK_SYMBOL_ADDED not in kinds  # the rename target is not reported as new
    assert event.target.package_name == "acme-ai" and event.target.modules == ("acme_ai", "acme_ai.legacy")
    assert event.source is ExternalChangeSource.SDK_SURFACE

    no_hints = _diff(old, new)
    plain_kinds = {i.kind for i in no_hints.diff}
    assert DiffItemKind.SDK_SYMBOL_REMOVED in plain_kinds and DiffItemKind.SDK_SYMBOL_ADDED in plain_kinds
    assert DiffItemKind.SDK_SYMBOL_RENAMED not in plain_kinds
    assert no_hints.fingerprint != event.fingerprint


def test_sdk_parameter_removed_and_became_required() -> None:
    old = _surface("1.0.0", {"x.run": {"params": {"a": {}, "b": {}}}})
    new = _surface("1.1.0", {"x.run": {"params": {"a": {"required": True}}}})
    event = _diff(old, new)
    kinds = {i.kind: i.compatibility for i in event.diff}
    assert kinds[DiffItemKind.SDK_PARAMETER_REMOVED] is BR
    assert kinds[DiffItemKind.SDK_PARAMETER_BECAME_REQUIRED] is BR
    assert kinds[DiffItemKind.PACKAGE_MINOR_BUMP] is NB


@pytest.mark.parametrize(
    ("old", "new", "kind", "compat"),
    [
        ("12.3.0", "13.0.0", DiffItemKind.PACKAGE_MAJOR_BUMP, PB),
        ("12.3.0", "12.4.0", DiffItemKind.PACKAGE_MINOR_BUMP, NB),
        ("0.4.1", "0.5.0", DiffItemKind.PACKAGE_MINOR_BUMP, PB),
        ("12.3.0", "12.3.1", DiffItemKind.PACKAGE_PATCH_BUMP, NB),
        ("12.3.0", "12.2.0", DiffItemKind.PACKAGE_DOWNGRADE, PB),
        ("1.2.0", "1.2.0rc1", DiffItemKind.PACKAGE_PRERELEASE_CHANGE, PB),
        ("latest", "13.0.0", DiffItemKind.PACKAGE_VERSION_UNPARSEABLE, UNK),
    ],
)
def test_version_diff_items(old: str, new: str, kind: DiffItemKind, compat: CompatibilityClass) -> None:
    (item,) = version_diff_items("stripe", old, new)
    assert (item.kind, item.compatibility) == (kind, compat)
    assert item.replacement == new


def test_version_spec_parsing() -> None:
    assert parse_version("^13.0.0") is not None and parse_version("^13.0.0").major == 13  # type: ignore[union-attr]
    assert parse_version(">=1.4,<2").minor == 4  # type: ignore[union-attr]
    assert parse_version("v2.1") is not None
    assert version_diff_items("x", "1.0.0", "1.0.0") == ()


def test_major_version_bump_without_structure_is_review_required_never_breaking() -> None:
    event = build_version_change(
        DependencyTarget(provider_key="stripe", ecosystem=Ecosystem.PYPI, package_name="stripe"), "12.3.0", "13.0.0"
    )
    assert event.kind is ExternalChangeKind.VERSION_UPDATE
    assert event.classification.risk is ChangeRisk.REVIEW_REQUIRED
    assert "major_version_bump_without_structural_proof" in event.classification.reasons
    assert not event.classification.has_structural_evidence
    patch = build_version_change(DependencyTarget(package_name="stripe"), "12.3.0", "12.3.1")
    assert patch.classification.risk is ChangeRisk.SAFE


def test_classifier_rules_and_reason_codes() -> None:
    new = _spec()
    new["paths"]["/payouts"] = {"get": {"responses": {"200": {"description": "ok"}}}}
    additive = _diff(_spec(), new)
    assert additive.classification.risk is ChangeRisk.LOW_RISK
    assert additive.classification.reasons == ("non_breaking_changes_only",)
    assert classify((), has_structural_evidence=True).risk is ChangeRisk.SAFE


# -- identity / idempotency ------------------------------------------------------


def test_event_fingerprint_is_stable_and_ignores_observation_time() -> None:
    new = _spec()
    del new["paths"]["/reports/{id}"]
    first = _diff(_spec(), new)
    second = _diff(_spec(), copy.deepcopy(new))
    assert first.fingerprint == second.fingerprint
    assert first.observed_at != second.observed_at or first.observed_at == second.observed_at
    assert [i.key for i in first.diff] == [i.key for i in second.diff]
    other = _spec()
    del other["paths"]["/charges/{id}/refund"]
    assert _diff(_spec(), other).fingerprint != first.fingerprint


def test_mixing_contract_formats_is_refused() -> None:
    with pytest.raises(ContractLoadError):
        build_contract_change(
            parse_contract_document(_spec(), ref="a"),
            parse_contract_document(_surface("1", {}), ref="b"),
        )
    with pytest.raises(ContractLoadError):
        parse_contract_document({"hello": "world"}, ref="c")
    with pytest.raises(SurfaceError):
        parse_surface({"patchfrog_sdk_surface": 1})


def test_release_metadata_digests_notes_and_sanitizes_url() -> None:
    release = release_from_metadata({
        "tag_name": "v13.0.0", "published_at": "2026-09-01T00:00:00Z",
        "html_url": "https://user:pw@github.com/acme/sdk/releases/tag/v13.0.0?token=abc",
        "body": "Breaking: removed X", "prerelease": False,
    })
    assert release.version == "13.0.0"
    assert release.url == "https://github.com/acme/sdk/releases/tag/v13.0.0"
    assert release.notes_sha256 is not None and "Breaking" not in repr(release)


# -- hints -----------------------------------------------------------------------


def test_hints_reject_credential_shaped_values_and_bad_shapes() -> None:
    for value in ("sk-live-0123456789abcdefghij", "ghp_0123456789abcdefghijklmnop", "AKIAABCDEFGHIJKLMNOP"):
        with pytest.raises(HintError):
            parse_hints({"patchfrog_change_hints": 1, "required_parameter_values": [
                {"symbol": "a.b", "parameter": "key", "value": value}]})
    with pytest.raises(HintError):
        parse_hints({"patchfrog_change_hints": 1, "required_parameter_values": [
            {"symbol": "a.b", "parameter": "x", "value": {"nested": 1}}]})
    with pytest.raises(HintError):
        parse_hints({"patchfrog_change_hints": 1, "required_parameter_values": [
            {"symbol": "a.b", "parameter": "x", "value": 1, "from_parameter": "y"}]})
    with pytest.raises(HintError):
        parse_hints({"patchfrog_change_hints": 1, "endpoint_replacements": [{"old": "/x", "new": "/y"}]})
    with pytest.raises(HintError):
        parse_hints({"not_hints": 1})
    ok = parse_hints({"patchfrog_change_hints": 1, "required_parameter_values": [
        {"symbol": "a.b", "parameter": "store", "value": False}]})
    assert ok.required_value("a.b", "store") is not None
    assert parse_hints(None).fingerprint() is None
