"""JSON Schema for :class:`~patchfrog.scalability.project.domain.ApplicationModel`
(Milestone 2.5).

The schema is **generated from the frozen dataclasses**, the same type hints
that :meth:`ApplicationModel.from_dict` uses to load an artifact, so there is
one source of truth and nothing to keep in sync by hand. The committed copy
in ``docs/schemas/`` is a snapshot that a test regenerates and compares
byte for byte.

Mapping (JSON Schema draft 2020-12):

* every dataclass is a closed object (``additionalProperties: false``), and
  every field is **required**, because serialization always writes every field;
  optional values are nullable (``X | None`` -> ``anyOf [X, null]``), never
  omitted;
* ``StrEnum`` -> ``string`` with the closed ``enum`` of values;
* ``tuple[X, ...]`` -> ``array`` of X (order is part of the canonical form);
* ``schema_version`` accepts the supported versions only (1 and 2); a newer
  artifact is invalid, and ``from_dict`` refuses it too. Version 1 artifacts
  have no ``database`` section; version 2 requires it.
"""

from __future__ import annotations

import json
import types
import typing
from dataclasses import fields, is_dataclass
from enum import StrEnum

from patchfrog.scalability.project.domain import DISCOVERY_SCHEMA_VERSION, ApplicationModel

SCHEMA_ID = f"urn:patchfrog:schema:application-model:{DISCOVERY_SCHEMA_VERSION}"
SUPPORTED_SCHEMA_VERSIONS: tuple[int, ...] = (1, DISCOVERY_SCHEMA_VERSION)
_PRIMITIVES: dict[object, dict[str, object]] = {
    str: {"type": "string"},
    int: {"type": "integer"},
    bool: {"type": "boolean"},
    float: {"type": "number"},
}


def _definition(annotation: object, defs: dict[str, dict[str, object]]) -> dict[str, object]:
    origin = typing.get_origin(annotation)
    if origin in (typing.Union, types.UnionType):
        options = [a for a in typing.get_args(annotation) if a is not type(None)]
        if len(options) != 1:
            raise TypeError(f"unsupported union {annotation!r}")
        return {"anyOf": [_definition(options[0], defs), {"type": "null"}]}
    if origin is tuple:
        item, _ellipsis = typing.get_args(annotation)
        return {"type": "array", "items": _definition(item, defs)}
    if isinstance(annotation, type) and issubclass(annotation, StrEnum):
        name = annotation.__name__
        defs.setdefault(name, {"type": "string", "enum": [member.value for member in annotation]})
        return {"$ref": f"#/$defs/{name}"}
    if isinstance(annotation, type) and is_dataclass(annotation):
        name = annotation.__name__
        if name not in defs:
            defs[name] = {}  # reserve first: guards recursion and keeps one definition per class
            hints = typing.get_type_hints(annotation)
            properties = {f.name: _definition(hints[f.name], defs) for f in fields(annotation)}
            defs[name] = {
                "type": "object",
                "additionalProperties": False,
                "required": [f.name for f in fields(annotation)],
                "properties": properties,
            }
        return {"$ref": f"#/$defs/{name}"}
    if annotation in _PRIMITIVES:
        return dict(_PRIMITIVES[annotation])
    raise TypeError(f"unsupported annotation {annotation!r}")


def application_model_json_schema() -> dict[str, object]:
    defs: dict[str, dict[str, object]] = {}
    _definition(ApplicationModel, defs)
    root = defs.pop("ApplicationModel")
    properties = dict(typing.cast(dict[str, object], root["properties"]))
    properties["schema_version"] = {"type": "integer", "enum": list(SUPPORTED_SCHEMA_VERSIONS)}
    required = [name for name in typing.cast(list[str], root["required"]) if name != "database"]
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": SCHEMA_ID,
        "title": "PatchFrog ApplicationModel",
        "description": (
            "Static application discovery result (discovery.json). Generated from "
            "patchfrog.scalability.project.domain; see docs/architecture/DISCOVERY.md."
        ),
        "type": "object",
        "additionalProperties": False,
        "required": required,
        "properties": properties,
        # Version 2 (Milestone 2.4) added the database section; version 1 has none.
        "if": {"properties": {"schema_version": {"const": 1}}},
        "then": {"not": {"required": ["database"]}},
        "else": {"required": ["database"]},
        "$defs": dict(sorted(defs.items())),
    }


def application_model_json_schema_text() -> str:
    """The canonical text of the schema (the committed snapshot)."""

    return json.dumps(application_model_json_schema(), indent=2, sort_keys=True) + "\n"


__all__ = [
    "SCHEMA_ID",
    "SUPPORTED_SCHEMA_VERSIONS",
    "application_model_json_schema",
    "application_model_json_schema_text",
]
