"""Local OpenAPI 3.x / Swagger 2.0 discovery, normalization and
fingerprinting (M5.4 / M5.10).

Normalization keeps exactly the *structure* a future compatibility diff
(M6) needs -- paths, methods, parameters, request bodies, responses,
security requirements, security schemes and component/definition
identities with their property shapes -- and drops everything that is
presentation or example data: descriptions, summaries, titles,
examples, ``x-`` extensions, server URLs' paths. Every mapping is
emitted with sorted keys and every set-like list sorted, so YAML vs
JSON, key order, list order of parameters/required names and whitespace
never change the fingerprint; a real path/schema/auth change does.

``$ref`` values are kept as component *identities*
(``#/components/schemas/Customer`` -> ``schema:Customer``), never
resolved across files or fetched over the network.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from pathlib import PurePosixPath
from typing import Any

import yaml

from patchfrog.dependencies.domain import CONTRACT_NORMALIZATION_VERSION, ContractFingerprint

_SPEC_NAME_RE = re.compile(
    r"^(openapi|swagger|api[-_.]?spec|api)([-_.][\w-]+)?\.(ya?ml|json)$|^[\w-]+\.openapi\.(ya?ml|json)$",
    re.IGNORECASE,
)
_HTTP_METHODS = ("get", "put", "post", "delete", "options", "head", "patch", "trace")
_SCHEMA_KEYS = (
    "type", "format", "enum", "required", "properties", "items", "additionalProperties", "allOf", "oneOf",
    "anyOf", "not", "nullable", "$ref", "minimum", "maximum", "minLength", "maxLength", "pattern", "const",
    "readOnly", "writeOnly", "deprecated", "discriminator",
)
_MAX_SCHEMA_DEPTH = 12


def looks_like_spec_path(path: str) -> bool:
    return bool(_SPEC_NAME_RE.match(PurePosixPath(path).name))


def load_spec(text: str) -> dict[str, Any] | None:
    """Parse YAML or JSON (``yaml.safe_load`` only -- no object
    construction). ``None`` unless the document declares
    ``openapi: 3.x`` or ``swagger: "2.0"``."""

    try:
        data = json.loads(text) if text.lstrip().startswith("{") else yaml.safe_load(text)
    except (json.JSONDecodeError, yaml.YAMLError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    version = str(data.get("openapi") or data.get("swagger") or "")
    if not (version.startswith("3.") or version.startswith("2.")):
        return None
    return data


def _ref_identity(ref: str) -> str:
    for prefix, kind in (
        ("#/components/schemas/", "schema"), ("#/definitions/", "schema"),
        ("#/components/parameters/", "parameter"), ("#/parameters/", "parameter"),
        ("#/components/responses/", "response"), ("#/responses/", "response"),
        ("#/components/requestBodies/", "request_body"), ("#/components/securitySchemes/", "security_scheme"),
    ):
        if ref.startswith(prefix):
            return f"{kind}:{ref[len(prefix):]}"
    return f"external:{ref.split('#', 1)[-1]}" if "#" in ref else "external"


def _schema(node: Any, depth: int = 0) -> Any:
    if depth > _MAX_SCHEMA_DEPTH:
        return {"truncated": True}
    if isinstance(node, list):
        return [_schema(item, depth + 1) for item in node]
    if not isinstance(node, Mapping):
        return node if isinstance(node, (str, int, float, bool)) or node is None else str(node)
    out: dict[str, Any] = {}
    for key in _SCHEMA_KEYS:
        if key not in node:
            continue
        value = node[key]
        if key == "$ref":
            out["ref"] = _ref_identity(str(value))
        elif key == "properties" and isinstance(value, Mapping):
            out["properties"] = {str(k): _schema(v, depth + 1) for k, v in sorted(value.items())}
        elif key in ("required", "enum") and isinstance(value, list):
            out[key] = sorted(str(v) for v in value)
        elif key in ("allOf", "oneOf", "anyOf") and isinstance(value, list):
            items = [_schema(v, depth + 1) for v in value]
            out[key] = sorted(items, key=lambda i: json.dumps(i, sort_keys=True))
        elif key == "discriminator" and isinstance(value, Mapping):
            out[key] = {"propertyName": value.get("propertyName")}
        else:
            out[key] = _schema(value, depth + 1)
    return out


def _parameter(param: Any) -> dict[str, Any]:
    if not isinstance(param, Mapping):
        return {}
    if "$ref" in param:
        return {"ref": _ref_identity(str(param["$ref"]))}
    schema = param.get("schema") if "schema" in param else {k: param[k] for k in ("type", "format", "items") if k in param}
    return {
        "name": str(param.get("name", "")),
        "in": str(param.get("in", "")),
        "required": bool(param.get("required", param.get("in") == "path")),
        "schema": _schema(schema or {}),
    }


def _content(node: Any) -> dict[str, Any]:
    if not isinstance(node, Mapping):
        return {}
    return {str(media): _schema((body or {}).get("schema", {})) for media, body in sorted(node.items())}


def _security(requirements: Any) -> list[Any]:
    if not isinstance(requirements, list):
        return []
    normalized = [
        {str(name): sorted(str(s) for s in (scopes or [])) for name, scopes in sorted(req.items())}
        for req in requirements
        if isinstance(req, Mapping)
    ]
    return sorted(normalized, key=lambda r: json.dumps(r, sort_keys=True))


def _operation(op: Mapping[str, Any], shared_params: list[Any], swagger2: bool) -> dict[str, Any]:
    params = {(p.get("name"), p.get("in")): p for p in shared_params if isinstance(p, Mapping)}
    for p in op.get("parameters", []) or []:
        if isinstance(p, Mapping):
            params[(p.get("name"), p.get("in"))] = p
    normalized_params = [_parameter(p) for p in params.values() if not (swagger2 and p.get("in") == "body")]
    body: dict[str, Any] | None = None
    if swagger2:
        body_param = next((p for p in params.values() if p.get("in") == "body"), None)
        if body_param is not None:
            body = {"required": bool(body_param.get("required", False)),
                    "content": {"application/json": _schema(body_param.get("schema", {}))}}
    elif isinstance(op.get("requestBody"), Mapping):
        rb = op["requestBody"]
        body = (
            {"ref": _ref_identity(str(rb["$ref"]))}
            if "$ref" in rb
            else {"required": bool(rb.get("required", False)), "content": _content(rb.get("content"))}
        )
    responses: dict[str, Any] = {}
    for status, response in sorted((op.get("responses") or {}).items(), key=lambda kv: str(kv[0])):
        if not isinstance(response, Mapping):
            continue
        if "$ref" in response:
            responses[str(status)] = {"ref": _ref_identity(str(response["$ref"]))}
        elif swagger2:
            responses[str(status)] = {"schema": _schema(response.get("schema", {}))}
        else:
            responses[str(status)] = {"content": _content(response.get("content"))}
    result: dict[str, Any] = {
        "parameters": sorted(normalized_params, key=lambda p: (str(p.get("in")), str(p.get("name")), str(p.get("ref")))),
        "request_body": body,
        "responses": responses,
        "deprecated": bool(op.get("deprecated", False)),
    }
    if "operationId" in op:
        result["operation_id"] = str(op["operationId"])
    if "security" in op:
        result["security"] = _security(op["security"])
    return result


def normalize_spec(spec: Mapping[str, Any]) -> dict[str, Any]:
    swagger2 = str(spec.get("swagger", "")).startswith("2.")
    paths: dict[str, Any] = {}
    for raw_path, item in sorted((spec.get("paths") or {}).items(), key=lambda kv: str(kv[0])):
        if not isinstance(item, Mapping):
            continue
        shared = list(item.get("parameters", []) or [])
        operations = {
            method: _operation(item[method], shared, swagger2)
            for method in _HTTP_METHODS
            if isinstance(item.get(method), Mapping)
        }
        if operations:
            paths[str(raw_path)] = operations
    if swagger2:
        schemas = spec.get("definitions") or {}
        schemes = spec.get("securityDefinitions") or {}
    else:
        components = spec.get("components") or {}
        schemas = components.get("schemas") or {}
        schemes = components.get("securitySchemes") or {}
    security_schemes = {
        str(name): {k: str(v) for k, v in sorted(scheme.items()) if k in ("type", "scheme", "in", "name", "bearerFormat", "flow")}
        for name, scheme in sorted(schemes.items())
        if isinstance(scheme, Mapping)
    }
    return {
        "format": "swagger2" if swagger2 else "openapi3",
        "paths": paths,
        "components": {str(name): _schema(schema) for name, schema in sorted(schemas.items())},
        "security": _security(spec.get("security")),
        "security_schemes": security_schemes,
    }


def server_hosts(spec: Mapping[str, Any]) -> tuple[str, ...]:
    """Hostnames only (Swagger ``host`` or OpenAPI ``servers[].url``)."""

    from patchfrog.dependencies.scan import url_host

    hosts: set[str] = set()
    if isinstance(spec.get("host"), str):
        hosts.add(spec["host"].split(":")[0].lower())
    for server in spec.get("servers", []) or []:
        if isinstance(server, Mapping) and isinstance(server.get("url"), str):
            host = url_host(server["url"])
            if host:
                hosts.add(host)
    return tuple(sorted(hosts))


def spec_title(spec: Mapping[str, Any]) -> str | None:
    info = spec.get("info")
    title = info.get("title") if isinstance(info, Mapping) else None
    return str(title)[:128] if title else None


def fingerprint_normalized(normalized: Mapping[str, Any]) -> ContractFingerprint:
    canonical = json.dumps(
        {"normalization_version": CONTRACT_NORMALIZATION_VERSION, "contract": normalized},
        sort_keys=True,
        separators=(",", ":"),
    )
    return ContractFingerprint(value=hashlib.sha256(canonical.encode()).hexdigest())


def summarize(normalized: Mapping[str, Any]) -> dict[str, int]:
    paths = normalized.get("paths") or {}
    return {
        "paths": len(paths),
        "operations": sum(len(ops) for ops in paths.values()),
        "schemas": len(normalized.get("components") or {}),
        "security_schemes": len(normalized.get("security_schemes") or {}),
    }


def path_pattern(path: str) -> re.Pattern[str]:
    """``/v1/customers/{id}`` -> a regex matching concrete or
    placeholder-normalized (``{}``) literals of that path."""

    parts = [r"(?:\{\}|[^/]+)" if seg.startswith("{") and seg.endswith("}") else re.escape(seg)
             for seg in path.strip("/").split("/")]
    return re.compile("^/" + "/".join(parts) + "/?$")


__all__ = [
    "fingerprint_normalized",
    "load_spec",
    "looks_like_spec_path",
    "normalize_spec",
    "path_pattern",
    "server_hosts",
    "spec_title",
    "summarize",
]
