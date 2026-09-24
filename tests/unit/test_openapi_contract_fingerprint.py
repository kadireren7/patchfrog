"""M5.4/M5.10: OpenAPI normalization and fingerprint stability."""

from __future__ import annotations

import copy
import json
from typing import Any

import yaml

from patchfrog.dependencies.openapi import (
    fingerprint_normalized,
    load_spec,
    looks_like_spec_path,
    normalize_spec,
    path_pattern,
    server_hosts,
    summarize,
)

_SPEC: dict[str, Any] = {
    "openapi": "3.0.3",
    "info": {"title": "Payments", "version": "2.1.0", "description": "Human text"},
    "servers": [{"url": "https://payments.example.com/api"}],
    "security": [{"bearer": []}],
    "paths": {
        "/v1/charges/{charge_id}": {
            "parameters": [{"name": "charge_id", "in": "path", "required": True, "schema": {"type": "string"}}],
            "get": {
                "operationId": "getCharge",
                "summary": "Get a charge",
                "parameters": [
                    {"name": "expand", "in": "query", "schema": {"type": "string"}},
                    {"name": "locale", "in": "query", "schema": {"type": "string"}},
                ],
                "responses": {
                    "200": {
                        "description": "ok",
                        "content": {"application/json": {"schema": {"$ref": "#/components/schemas/Charge"},
                                                         "example": {"id": "ch_1"}}},
                    }
                },
            },
        }
    },
    "components": {
        "securitySchemes": {"bearer": {"type": "http", "scheme": "bearer", "description": "JWT"}},
        "schemas": {
            "Charge": {
                "type": "object",
                "required": ["id", "amount"],
                "properties": {"id": {"type": "string"}, "amount": {"type": "integer", "description": "cents"}},
            }
        },
    },
}


def _fingerprint(document: dict[str, Any]) -> str:
    return fingerprint_normalized(normalize_spec(document)).value


def test_yaml_and_json_forms_of_the_same_contract_share_a_fingerprint() -> None:
    from_yaml = load_spec(yaml.safe_dump(_SPEC, sort_keys=False))
    from_json = load_spec(json.dumps(_SPEC, indent=4))
    assert from_yaml is not None and from_json is not None
    assert _fingerprint(from_yaml) == _fingerprint(from_json)


def test_key_order_list_order_whitespace_and_prose_do_not_change_the_fingerprint() -> None:
    reordered = copy.deepcopy(_SPEC)
    op = reordered["paths"]["/v1/charges/{charge_id}"]["get"]
    op["parameters"].reverse()
    reordered["components"]["schemas"]["Charge"]["required"].reverse()
    reordered = json.loads(json.dumps(reordered, sort_keys=True))
    reordered["info"]["description"] = "Totally different prose"
    op = reordered["paths"]["/v1/charges/{charge_id}"]["get"]
    op["summary"] = "renamed summary"
    op["responses"]["200"]["content"]["application/json"]["example"] = {"id": "ch_2"}
    assert _fingerprint(reordered) == _fingerprint(_SPEC)


def test_meaningful_path_change_changes_the_fingerprint() -> None:
    changed = copy.deepcopy(_SPEC)
    changed["paths"]["/v2/charges/{charge_id}"] = changed["paths"].pop("/v1/charges/{charge_id}")
    assert _fingerprint(changed) != _fingerprint(_SPEC)


def test_meaningful_schema_change_changes_the_fingerprint() -> None:
    changed = copy.deepcopy(_SPEC)
    changed["components"]["schemas"]["Charge"]["properties"]["amount"]["type"] = "string"
    assert _fingerprint(changed) != _fingerprint(_SPEC)
    required = copy.deepcopy(_SPEC)
    required["components"]["schemas"]["Charge"]["required"] = ["id"]
    assert _fingerprint(required) != _fingerprint(_SPEC)


def test_auth_security_change_changes_the_fingerprint() -> None:
    changed = copy.deepcopy(_SPEC)
    changed["components"]["securitySchemes"]["bearer"] = {"type": "apiKey", "in": "header", "name": "X-Key"}
    assert _fingerprint(changed) != _fingerprint(_SPEC)
    per_operation = copy.deepcopy(_SPEC)
    per_operation["paths"]["/v1/charges/{charge_id}"]["get"]["security"] = []
    assert _fingerprint(per_operation) != _fingerprint(_SPEC)


def test_normalized_contract_keeps_structure_and_drops_examples_and_descriptions() -> None:
    normalized = normalize_spec(_SPEC)
    rendered = json.dumps(normalized)
    assert "ch_1" not in rendered and "Human text" not in rendered and "cents" not in rendered
    operation = normalized["paths"]["/v1/charges/{charge_id}"]["get"]
    assert {p["name"] for p in operation["parameters"]} == {"charge_id", "expand", "locale"}
    assert operation["responses"]["200"]["content"]["application/json"] == {"ref": "schema:Charge"}
    assert summarize(normalized) == {"paths": 1, "operations": 1, "schemas": 1, "security_schemes": 1}
    assert server_hosts(_SPEC) == ("payments.example.com",)


def test_swagger_2_is_supported() -> None:
    swagger = {
        "swagger": "2.0",
        "host": "legacy.example.com",
        "paths": {"/items": {"post": {"parameters": [{"name": "body", "in": "body", "schema": {"type": "object"}}],
                                      "responses": {"201": {"description": "c"}}}}},
        "definitions": {"Item": {"type": "object"}},
    }
    normalized = normalize_spec(swagger)
    assert normalized["format"] == "swagger2"
    assert normalized["paths"]["/items"]["post"]["request_body"]["content"] == {"application/json": {"type": "object"}}
    assert server_hosts(swagger) == ("legacy.example.com",)


def test_non_spec_documents_are_rejected_and_spec_names_recognized() -> None:
    assert load_spec("name: not an api\n") is None
    assert load_spec("{not json") is None
    assert looks_like_spec_path("api/openapi.yaml") and looks_like_spec_path("swagger.json")
    assert looks_like_spec_path("payments.openapi.yml") and not looks_like_spec_path("docker-compose.yml")


def test_path_patterns_match_concrete_and_placeholder_literals() -> None:
    pattern = path_pattern("/v1/charges/{charge_id}")
    assert pattern.match("/v1/charges/ch_123") and pattern.match("/v1/charges/{}") and pattern.match("/v1/charges/{id}")
    assert not pattern.match("/v1/charges") and not pattern.match("/v1/refunds/ch_1")
