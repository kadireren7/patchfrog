"""Deterministic OpenAPI contract diff (M6.2).

Runs over two contracts already normalized by
:func:`patchfrog.dependencies.openapi.normalize_spec` -- the same
structure the M5 registry fingerprints and stores -- so a registry
snapshot and a freshly-loaded spec diff identically. Nothing is parsed
twice and nothing is fetched.

Compatibility is judged from the **consumer's** side and is
direction-aware:

- request side (parameters, request bodies, schemas only used in
  requests): new *required* inputs break consumers; removed inputs are
  only potentially breaking; narrowed enums break; widened ones do not.
- response side (responses, schemas only used in responses): removed
  fields break consumers; new fields do not; expanded enums are
  potentially breaking (unhandled values); fields becoming optional are
  potentially breaking.
- a component schema used on both sides takes the more severe verdict.

Renames/replacements are never guessed: a moved endpoint is recognised
only by an unchanged ``operationId`` or an explicit hint.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from patchfrog.upstream.domain import (
    MAX_REPRESENTATION_CHARS,
    CompatibilityClass,
    ContractDiffItem,
    DiffItemKind,
    DiffSubject,
    SubjectKind,
    most_severe,
)
from patchfrog.upstream.hints import EMPTY_HINTS, ChangeHints, operation_label, parse_operation

NB = CompatibilityClass.NON_BREAKING
PB = CompatibilityClass.POTENTIALLY_BREAKING
BR = CompatibilityClass.BREAKING
UNK = CompatibilityClass.UNKNOWN

_PLACEHOLDER_RE = re.compile(r"\{[^}]*\}")
_NUMERIC = frozenset({"integer", "number"})
_CONSTRAINT_KEYS = ("minimum", "maximum", "minLength", "maxLength", "pattern", "format", "const")
_COMPOSITION_KEYS = ("allOf", "oneOf", "anyOf", "not", "discriminator")
_MAX_SCHEMA_DIFF_DEPTH = 8


class Direction(StrEnum):
    REQUEST = "request"
    RESPONSE = "response"
    BOTH = "both"


class _Ctx(StrEnum):
    PARAMETER = "parameter"
    REQUEST = "request"
    RESPONSE = "response"
    SCHEMA = "schema"


# (context, schema-change) -> item kind. Schema changes are expressed
# once, then named per context.
_KIND: dict[tuple[_Ctx, str], DiffItemKind] = {
    (_Ctx.PARAMETER, "type"): DiffItemKind.PARAMETER_TYPE_CHANGED,
    (_Ctx.PARAMETER, "enum_narrowed"): DiffItemKind.PARAMETER_ENUM_NARROWED,
    (_Ctx.PARAMETER, "enum_expanded"): DiffItemKind.PARAMETER_ENUM_EXPANDED,
    (_Ctx.PARAMETER, "schema"): DiffItemKind.PARAMETER_SCHEMA_CHANGED,
    (_Ctx.PARAMETER, "field_added_required"): DiffItemKind.PARAMETER_SCHEMA_CHANGED,
    (_Ctx.PARAMETER, "field_added_optional"): DiffItemKind.PARAMETER_SCHEMA_CHANGED,
    (_Ctx.PARAMETER, "field_removed"): DiffItemKind.PARAMETER_SCHEMA_CHANGED,
    (_Ctx.PARAMETER, "field_became_required"): DiffItemKind.PARAMETER_SCHEMA_CHANGED,
    (_Ctx.PARAMETER, "field_became_optional"): DiffItemKind.PARAMETER_SCHEMA_CHANGED,
    (_Ctx.REQUEST, "type"): DiffItemKind.REQUEST_TYPE_CHANGED,
    (_Ctx.REQUEST, "enum_narrowed"): DiffItemKind.REQUEST_ENUM_NARROWED,
    (_Ctx.REQUEST, "enum_expanded"): DiffItemKind.REQUEST_ENUM_EXPANDED,
    (_Ctx.REQUEST, "schema"): DiffItemKind.REQUEST_SCHEMA_CHANGED,
    (_Ctx.REQUEST, "field_added_required"): DiffItemKind.REQUEST_FIELD_ADDED_REQUIRED,
    (_Ctx.REQUEST, "field_added_optional"): DiffItemKind.REQUEST_FIELD_ADDED_OPTIONAL,
    (_Ctx.REQUEST, "field_removed"): DiffItemKind.REQUEST_FIELD_REMOVED,
    (_Ctx.REQUEST, "field_became_required"): DiffItemKind.REQUEST_FIELD_BECAME_REQUIRED,
    (_Ctx.REQUEST, "field_became_optional"): DiffItemKind.REQUEST_FIELD_BECAME_OPTIONAL,
    (_Ctx.RESPONSE, "type"): DiffItemKind.RESPONSE_TYPE_CHANGED,
    (_Ctx.RESPONSE, "enum_narrowed"): DiffItemKind.RESPONSE_ENUM_NARROWED,
    (_Ctx.RESPONSE, "enum_expanded"): DiffItemKind.RESPONSE_ENUM_EXPANDED,
    (_Ctx.RESPONSE, "schema"): DiffItemKind.RESPONSE_SCHEMA_CHANGED,
    (_Ctx.RESPONSE, "field_added_required"): DiffItemKind.RESPONSE_FIELD_ADDED,
    (_Ctx.RESPONSE, "field_added_optional"): DiffItemKind.RESPONSE_FIELD_ADDED,
    (_Ctx.RESPONSE, "field_removed"): DiffItemKind.RESPONSE_FIELD_REMOVED,
    (_Ctx.RESPONSE, "field_became_required"): DiffItemKind.RESPONSE_FIELD_BECAME_REQUIRED,
    (_Ctx.RESPONSE, "field_became_optional"): DiffItemKind.RESPONSE_FIELD_BECAME_OPTIONAL,
    (_Ctx.SCHEMA, "type"): DiffItemKind.SCHEMA_TYPE_CHANGED,
    (_Ctx.SCHEMA, "enum_narrowed"): DiffItemKind.SCHEMA_ENUM_NARROWED,
    (_Ctx.SCHEMA, "enum_expanded"): DiffItemKind.SCHEMA_ENUM_EXPANDED,
    (_Ctx.SCHEMA, "schema"): DiffItemKind.SCHEMA_CHANGED,
    (_Ctx.SCHEMA, "field_added_required"): DiffItemKind.SCHEMA_PROPERTY_ADDED_REQUIRED,
    (_Ctx.SCHEMA, "field_added_optional"): DiffItemKind.SCHEMA_PROPERTY_ADDED_OPTIONAL,
    (_Ctx.SCHEMA, "field_removed"): DiffItemKind.SCHEMA_PROPERTY_REMOVED,
    (_Ctx.SCHEMA, "field_became_required"): DiffItemKind.SCHEMA_PROPERTY_BECAME_REQUIRED,
    (_Ctx.SCHEMA, "field_became_optional"): DiffItemKind.SCHEMA_PROPERTY_BECAME_OPTIONAL,
}

# change -> (verdict when the value flows consumer->API, verdict API->consumer)
_VERDICT: dict[str, tuple[CompatibilityClass, CompatibilityClass]] = {
    "field_added_required": (BR, NB),
    "field_added_optional": (NB, NB),
    "field_removed": (PB, BR),
    "field_became_required": (BR, NB),
    "field_became_optional": (NB, PB),
    "enum_narrowed": (BR, NB),
    "enum_expanded": (NB, PB),
    "type": (BR, BR),
    "widened_numeric": (NB, BR),
    "narrowed_numeric": (BR, NB),
    "nullable_added": (NB, PB),
    "nullable_removed": (PB, NB),
    "constraint": (PB, NB),
    "ref": (PB, PB),
    "composition": (UNK, UNK),
    "truncated": (UNK, UNK),
}


def _verdict(change: str, direction: Direction) -> CompatibilityClass:
    request, response = _VERDICT[change]
    if direction is Direction.REQUEST:
        return request
    if direction is Direction.RESPONSE:
        return response
    return most_severe(request, response)


def canonical(value: Any) -> str | None:
    if value is None:
        return None
    rendered = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return rendered if len(rendered) <= MAX_REPRESENTATION_CHARS else rendered[: MAX_REPRESENTATION_CHARS - 3] + "..."


def template_key(path: str) -> str:
    """``/users/{id}`` and ``/users/{userId}`` are the same endpoint."""

    return _PLACEHOLDER_RE.sub("{}", path)


# -- component direction ------------------------------------------------------


def _refs(node: Any) -> set[str]:
    found: set[str] = set()
    if isinstance(node, Mapping):
        ref = node.get("ref")
        if isinstance(ref, str) and ref.startswith("schema:"):
            found.add(ref.removeprefix("schema:"))
        for value in node.values():
            found |= _refs(value)
    elif isinstance(node, list):
        for value in node:
            found |= _refs(value)
    return found


def _closure(names: set[str], components: Mapping[str, Any]) -> set[str]:
    seen: set[str] = set()
    pending = list(names)
    while pending:
        name = pending.pop()
        if name in seen:
            continue
        seen.add(name)
        pending.extend(_refs(components.get(name, {})) - seen)
    return seen


def operation_schema_refs(operation: Mapping[str, Any], components: Mapping[str, Any]) -> tuple[set[str], set[str]]:
    """(request-side, response-side) component names an operation uses,
    transitively."""

    request = _refs(operation.get("parameters")) | _refs(operation.get("request_body"))
    response = _refs(operation.get("responses"))
    return _closure(request, components), _closure(response, components)


def component_directions(contract: Mapping[str, Any]) -> dict[str, Direction]:
    components = contract.get("components") or {}
    request: set[str] = set()
    response: set[str] = set()
    for operations in (contract.get("paths") or {}).values():
        for operation in operations.values():
            req, resp = operation_schema_refs(operation, components)
            request |= req
            response |= resp
    directions: dict[str, Direction] = {}
    for name in components:
        if name in request and name not in response:
            directions[name] = Direction.REQUEST
        elif name in response and name not in request:
            directions[name] = Direction.RESPONSE
        else:
            # Used on both sides, or referenced by no operation at all:
            # the conservative verdict.
            directions[name] = Direction.BOTH
    return directions


def operations_using_schema(contract: Mapping[str, Any], schema: str) -> tuple[tuple[str, str], ...]:
    components = contract.get("components") or {}
    found: set[tuple[str, str]] = set()
    for path, operations in (contract.get("paths") or {}).items():
        for method, operation in operations.items():
            req, resp = operation_schema_refs(operation, components)
            if schema in req or schema in resp:
                found.add((method, path))
    return tuple(sorted(found))


def operations_using_scheme(contract: Mapping[str, Any], scheme: str) -> tuple[tuple[str, str], ...]:
    global_security = contract.get("security") or []
    found: set[tuple[str, str]] = set()
    for path, operations in (contract.get("paths") or {}).items():
        for method, operation in operations.items():
            requirements = operation.get("security", global_security)
            if any(scheme in requirement for requirement in requirements):
                found.add((method, path))
    return tuple(sorted(found))


# -- the diff ---------------------------------------------------------------


@dataclass
class _Builder:
    old: Mapping[str, Any]
    new: Mapping[str, Any]
    hints: ChangeHints
    items: list[ContractDiffItem] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def add(
        self,
        kind: DiffItemKind,
        location: str,
        subject: DiffSubject,
        compatibility: CompatibilityClass,
        old: Any,
        new: Any,
        explanation: str,
        evidence: Iterable[str],
        replacement: str | None = None,
    ) -> None:
        self.items.append(
            ContractDiffItem(
                kind=kind, location=location, subject=subject, compatibility=compatibility,
                old=canonical(old), new=canonical(new), explanation=explanation,
                evidence=tuple(sorted(set(evidence))), replacement=replacement,
            )
        )

    # -- schemas --------------------------------------------------------

    def schema(
        self,
        old: Any,
        new: Any,
        *,
        ctx: _Ctx,
        direction: Direction,
        location: str,
        subject: DiffSubject,
        depth: int = 0,
    ) -> None:
        if old == new:
            return
        if not isinstance(old, Mapping) or not isinstance(new, Mapping):
            self._schema_item("schema", ctx, direction, location, subject, old, new,
                              "schema shape changed", ["schema.shape_changed"])
            return
        if depth > _MAX_SCHEMA_DIFF_DEPTH or old.get("truncated") or new.get("truncated"):
            self._schema_item("truncated", ctx, direction, location, subject, old, new,
                              "schema too deep to compare deterministically", ["schema.truncated"])
            return

        old_ref, new_ref = old.get("ref"), new.get("ref")
        if old_ref or new_ref:
            if old_ref != new_ref:
                resolved = self._resolve_pair(old_ref, new_ref)
                if resolved is not None:
                    # Compare what the refs point at, in *this* direction.
                    self.schema(resolved[0], resolved[1], ctx=ctx, direction=direction, location=location,
                                subject=subject, depth=depth + 1)
                else:
                    self._schema_item("ref", ctx, direction, location, subject, old_ref, new_ref,
                                      f"schema reference changed from {old_ref} to {new_ref}",
                                      [f"old.ref={old_ref}", f"new.ref={new_ref}"])
            # Same ref: the component-level diff reports it (once).
            return

        old_type, new_type = old.get("type"), new.get("type")
        if old_type != new_type:
            if old_type in _NUMERIC and new_type in _NUMERIC:
                change = "widened_numeric" if new_type == "number" else "narrowed_numeric"
            else:
                change = "type"
            self._schema_item(change, ctx, direction, location, subject, old_type, new_type,
                              f"type changed from {old_type} to {new_type}",
                              [f"old.type={old_type}", f"new.type={new_type}"], suffix="type")
            return  # anything nested under a different type is moot

        old_enum, new_enum = old.get("enum"), new.get("enum")
        if old_enum != new_enum:
            removed = sorted(set(old_enum or []) - set(new_enum or [])) if old_enum else []
            added = sorted(set(new_enum or []) - set(old_enum or [])) if new_enum else []
            if old_enum is None and new_enum is not None:
                self._schema_item("enum_narrowed", ctx, direction, f"{location}.enum", subject, None, new_enum,
                                  "value constrained to an enum", ["enum.introduced"], suffix="enum_narrowed")
            elif old_enum is not None and new_enum is None:
                self._schema_item("enum_expanded", ctx, direction, f"{location}.enum", subject, old_enum, None,
                                  "enum constraint removed", ["enum.removed"], suffix="enum_expanded")
            else:
                if removed:
                    self._schema_item("enum_narrowed", ctx, direction, f"{location}.enum", subject, old_enum,
                                      new_enum, f"enum values removed: {', '.join(removed)}",
                                      [f"enum.removed={v}" for v in removed], suffix="enum_narrowed")
                if added:
                    self._schema_item("enum_expanded", ctx, direction, f"{location}.enum", subject, old_enum,
                                      new_enum, f"enum values added: {', '.join(added)}",
                                      [f"enum.added={v}" for v in added], suffix="enum_expanded")

        if bool(old.get("nullable")) != bool(new.get("nullable")):
            change = "nullable_added" if new.get("nullable") else "nullable_removed"
            self._schema_item(change, ctx, direction, f"{location}.nullable", subject, old.get("nullable"),
                              new.get("nullable"), f"nullability changed ({change})",
                              [f"old.nullable={bool(old.get('nullable'))}", f"new.nullable={bool(new.get('nullable'))}"],
                              suffix="schema")

        constraints = [k for k in _CONSTRAINT_KEYS if old.get(k) != new.get(k)]
        if constraints:
            self._schema_item("constraint", ctx, direction, location, subject,
                              {k: old.get(k) for k in constraints}, {k: new.get(k) for k in constraints},
                              f"constraints changed: {', '.join(constraints)}",
                              [f"constraint.changed={k}" for k in constraints], suffix="schema")

        compositions = [k for k in _COMPOSITION_KEYS if old.get(k) != new.get(k)]
        if compositions:
            self._schema_item("composition", ctx, direction, location, subject,
                              {k: old.get(k) for k in compositions}, {k: new.get(k) for k in compositions},
                              f"composed schema changed ({', '.join(compositions)}); compatibility not decidable",
                              [f"composition.changed={k}" for k in compositions], suffix="schema")

        self._properties(old, new, ctx=ctx, direction=direction, location=location, subject=subject, depth=depth)

        if old.get("items") != new.get("items") and (old.get("items") is not None or new.get("items") is not None):
            self.schema(old.get("items") or {}, new.get("items") or {}, ctx=ctx, direction=direction,
                        location=f"{location}.items", subject=subject, depth=depth + 1)

    def _properties(
        self,
        old: Mapping[str, Any],
        new: Mapping[str, Any],
        *,
        ctx: _Ctx,
        direction: Direction,
        location: str,
        subject: DiffSubject,
        depth: int,
    ) -> None:
        old_props: Mapping[str, Any] = old.get("properties") or {}
        new_props: Mapping[str, Any] = new.get("properties") or {}
        old_required = set(old.get("required") or [])
        new_required = set(new.get("required") or [])
        member_prefix = f"{subject.member}." if subject.member else ""
        for name in sorted(set(old_props) | set(new_props)):
            where = f"{location}.properties[{name}]"
            member = DiffSubject(subject.kind, subject.method, subject.path, subject.name, f"{member_prefix}{name}")
            if name not in old_props:
                change = "field_added_required" if name in new_required else "field_added_optional"
                self._schema_item(change, ctx, direction, where, member, None, new_props[name],
                                  f"{'required' if name in new_required else 'optional'} field '{name}' added",
                                  [f"new.properties.{name}", f"new.required={name in new_required}"], suffix=change)
            elif name not in new_props:
                self._schema_item("field_removed", ctx, direction, where, member, old_props[name], None,
                                  f"field '{name}' removed", [f"old.properties.{name}"], suffix="field_removed")
            else:
                if name not in old_required and name in new_required:
                    self._schema_item("field_became_required", ctx, direction, where, member, False, True,
                                      f"field '{name}' became required", [f"required.added={name}"],
                                      suffix="field_became_required")
                elif name in old_required and name not in new_required:
                    self._schema_item("field_became_optional", ctx, direction, where, member, True, False,
                                      f"field '{name}' became optional", [f"required.removed={name}"],
                                      suffix="field_became_optional")
                self.schema(old_props[name], new_props[name], ctx=ctx, direction=direction, location=where,
                            subject=member, depth=depth + 1)

    def _schema_item(
        self,
        change: str,
        ctx: _Ctx,
        direction: Direction,
        location: str,
        subject: DiffSubject,
        old: Any,
        new: Any,
        explanation: str,
        evidence: list[str],
        *,
        suffix: str = "schema",
    ) -> None:
        kind = _KIND[(ctx, suffix)]
        compatibility = _verdict(change, direction)
        self.add(kind, location, subject, compatibility, old, new, explanation,
                 [*evidence, f"direction={direction.value}"])

    def _resolve_pair(self, old_ref: Any, new_ref: Any) -> tuple[Any, Any] | None:
        if not (isinstance(old_ref, str) and isinstance(new_ref, str)):
            return None
        if not (old_ref.startswith("schema:") and new_ref.startswith("schema:")):
            return None
        old_schema = (self.old.get("components") or {}).get(old_ref.removeprefix("schema:"))
        new_schema = (self.new.get("components") or {}).get(new_ref.removeprefix("schema:"))
        if old_schema is None or new_schema is None:
            return None
        return old_schema, new_schema

    # -- operations -----------------------------------------------------

    def operations(self) -> None:
        old_paths: Mapping[str, Mapping[str, Any]] = self.old.get("paths") or {}
        new_paths: Mapping[str, Mapping[str, Any]] = self.new.get("paths") or {}
        old_ops = {(m, template_key(p)): (p, op) for p, ops in old_paths.items() for m, op in ops.items()}
        new_ops = {(m, template_key(p)): (p, op) for p, ops in new_paths.items() for m, op in ops.items()}
        old_path_keys = {template_key(p) for p in old_paths}
        new_path_keys = {template_key(p) for p in new_paths}
        new_by_operation_id = {
            (m, op["operation_id"]): key for key, (_, op) in new_ops.items() for m in [key[0]] if op.get("operation_id")
        }
        moved_targets: set[tuple[str, str]] = set()

        for key in sorted(old_ops):
            method, _ = key
            old_path, old_op = old_ops[key]
            if key in new_ops:
                new_path, new_op = new_ops[key]
                self.operation(method, old_path, old_op, new_op, location_path=new_path)
                continue
            replacement_key = self._replacement(method, old_path, old_op, new_ops, new_by_operation_id)
            subject = DiffSubject(SubjectKind.OPERATION, method=method, path=old_path)
            location = f"paths[{old_path}].{method}"
            if replacement_key is not None:
                moved_targets.add(replacement_key)
                new_path, new_op = new_ops[replacement_key]
                via = (
                    "operation_id" if old_op.get("operation_id")
                    and old_op.get("operation_id") == new_op.get("operation_id") else "hint"
                )
                self.add(
                    DiffItemKind.ENDPOINT_PATH_CHANGED, location, subject, BR,
                    operation_label(method, old_path), operation_label(replacement_key[0], new_path),
                    f"{operation_label(method, old_path)} moved to {operation_label(replacement_key[0], new_path)}",
                    [f"replacement.via={via}", f"old.path={old_path}", f"new.path={new_path}"],
                    replacement=operation_label(replacement_key[0], new_path),
                )
                self.operation(method, old_path, old_op, new_op, location_path=new_path, subject_path=old_path)
                continue
            whole_path_gone = template_key(old_path) not in new_path_keys
            self.add(
                DiffItemKind.ENDPOINT_REMOVED if whole_path_gone else DiffItemKind.OPERATION_REMOVED,
                location, subject, BR, {"operation_id": old_op.get("operation_id")}, None,
                f"{operation_label(method, old_path)} removed with no known replacement",
                ["path.removed" if whole_path_gone else "method.removed"],
            )

        for key in sorted(new_ops):
            if key in old_ops or key in moved_targets:
                continue
            method, _ = key
            new_path, new_op = new_ops[key]
            whole_path_new = template_key(new_path) not in old_path_keys
            self.add(
                DiffItemKind.ENDPOINT_ADDED if whole_path_new else DiffItemKind.OPERATION_ADDED,
                f"paths[{new_path}].{method}", DiffSubject(SubjectKind.OPERATION, method=method, path=new_path),
                NB, None, {"operation_id": new_op.get("operation_id")},
                f"{operation_label(method, new_path)} added", ["path.added" if whole_path_new else "method.added"],
            )

    def _replacement(
        self,
        method: str,
        old_path: str,
        old_op: Mapping[str, Any],
        new_ops: Mapping[tuple[str, str], tuple[str, Any]],
        new_by_operation_id: Mapping[tuple[str, str], tuple[str, str]],
    ) -> tuple[str, str] | None:
        hinted = self.hints.endpoint_replacement(operation_label(method, old_path))
        if hinted is not None:
            new_method, new_path = parse_operation(hinted)
            key = (new_method, template_key(new_path))
            if key in new_ops:
                return key
            self.notes.append(f"hint endpoint_replacement {hinted!r} is not in the new contract; ignored")
        operation_id = old_op.get("operation_id")
        if operation_id:
            return new_by_operation_id.get((method, operation_id))
        return None

    def operation(
        self,
        method: str,
        old_path: str,
        old: Mapping[str, Any],
        new: Mapping[str, Any],
        *,
        location_path: str,
        subject_path: str | None = None,
    ) -> None:
        path = subject_path or old_path
        base = f"paths[{location_path}].{method}"
        op_subject = DiffSubject(SubjectKind.OPERATION, method=method, path=path)
        label = operation_label(method, path)

        if not old.get("deprecated") and new.get("deprecated"):
            self.add(DiffItemKind.OPERATION_DEPRECATED, f"{base}.deprecated", op_subject, NB, False, True,
                     f"{label} deprecated", ["new.deprecated=true"])

        self._parameters(label, method, path, base, old.get("parameters") or [], new.get("parameters") or [],
                         old_template=old_path, new_template=location_path)
        self._request_body(label, method, path, base, old.get("request_body"), new.get("request_body"))
        self._responses(label, method, path, base, old.get("responses") or {}, new.get("responses") or {})
        if "security" in old or "security" in new:
            old_security = old["security"] if "security" in old else (self.old.get("security") or [])
            new_security = new["security"] if "security" in new else (self.new.get("security") or [])
            self.security(old_security, new_security, f"{base}.security", op_subject, label)

    def _parameters(
        self,
        label: str,
        method: str,
        path: str,
        base: str,
        old: list[Any],
        new: list[Any],
        *,
        old_template: str,
        new_template: str,
    ) -> None:
        def ident(param: Mapping[str, Any], template: str) -> tuple[str, str]:
            if "ref" in param:
                return ("ref", str(param["ref"]))
            name = str(param.get("name"))
            if param.get("in") == "path":
                # Path parameters are positional: ``{id}`` -> ``{chargeId}`` is
                # invisible to a consumer that builds the URL.
                placeholders = _PLACEHOLDER_RE.findall(template)
                if "{" + name + "}" in placeholders:
                    return ("path", f"#{placeholders.index('{' + name + '}')}")
            return (str(param.get("in")), name)

        old_by = {ident(p, old_template): p for p in old}
        new_by = {ident(p, new_template): p for p in new}
        renamed_to: dict[tuple[str, str], tuple[str, str]] = {}
        for location, name in old_by:
            if (location, name) in new_by or location == "ref":
                continue
            target = self.hints.operation_parameter_rename(label, location, name)
            if target is not None and (location, target) in new_by:
                renamed_to[(location, name)] = (location, target)

        for key in sorted(set(old_by) | set(new_by)):
            location = key[0]
            name = str((old_by.get(key) or new_by.get(key) or {}).get("name", key[1]))
            where = f"{base}.parameters[{location}:{key[1]}]"
            subject = DiffSubject(SubjectKind.OPERATION, method=method, path=path, member=name)
            if key in renamed_to:
                new_key = renamed_to[key]
                self.add(DiffItemKind.PARAMETER_RENAMED, where, subject, BR, old_by[key], new_by[new_key],
                         f"{location} parameter '{name}' renamed to '{new_key[1]}'",
                         [f"hint.parameter_rename={name}->{new_key[1]}", f"in={location}"], replacement=new_key[1])
                self._parameter_pair(old_by[key], new_by[new_key], where, subject)
                continue
            if key in renamed_to.values():
                continue
            if key not in old_by:
                param = new_by[key]
                if location == "ref":
                    self.add(DiffItemKind.PARAMETER_SCHEMA_CHANGED, where, subject, UNK, None, param,
                             "referenced parameter added (definition not in the normalized contract)",
                             ["parameter.ref.added"])
                elif param.get("required"):
                    self.add(DiffItemKind.PARAMETER_ADDED_REQUIRED, where, subject, BR, None, param,
                             f"required {location} parameter '{name}' added", ["new.required=true", f"in={location}"])
                else:
                    self.add(DiffItemKind.PARAMETER_ADDED_OPTIONAL, where, subject, NB, None, param,
                             f"optional {location} parameter '{name}' added", ["new.required=false", f"in={location}"])
            elif key not in new_by:
                param = old_by[key]
                if location == "ref":
                    self.add(DiffItemKind.PARAMETER_SCHEMA_CHANGED, where, subject, UNK, param, None,
                             "referenced parameter removed", ["parameter.ref.removed"])
                else:
                    self.add(DiffItemKind.PARAMETER_REMOVED, where, subject, PB, param, None,
                             f"{location} parameter '{name}' removed; callers still sending it may be rejected",
                             [f"old.required={bool(param.get('required'))}", f"in={location}"])
            else:
                self._parameter_pair(old_by[key], new_by[key], where, subject)

    def _parameter_pair(self, old: Mapping[str, Any], new: Mapping[str, Any], where: str, subject: DiffSubject) -> None:
        name = subject.member
        if not old.get("required") and new.get("required"):
            self.add(DiffItemKind.PARAMETER_BECAME_REQUIRED, f"{where}.required", subject, BR, False, True,
                     f"parameter '{name}' changed from optional to required", ["old.required=false", "new.required=true"])
        elif old.get("required") and not new.get("required"):
            self.add(DiffItemKind.PARAMETER_BECAME_OPTIONAL, f"{where}.required", subject, NB, True, False,
                     f"parameter '{name}' changed from required to optional", ["old.required=true", "new.required=false"])
        self.schema(old.get("schema") or {}, new.get("schema") or {}, ctx=_Ctx.PARAMETER, direction=Direction.REQUEST,
                    location=f"{where}.schema", subject=subject)

    def _request_body(self, label: str, method: str, path: str, base: str, old: Any, new: Any) -> None:
        where = f"{base}.request_body"
        subject = DiffSubject(SubjectKind.OPERATION, method=method, path=path)
        if old == new:
            return
        if old is None:
            required = bool((new or {}).get("required"))
            self.add(DiffItemKind.REQUEST_BODY_ADDED_REQUIRED if required else DiffItemKind.REQUEST_BODY_ADDED_OPTIONAL,
                     where, subject, BR if required else NB, None, new,
                     f"{'required' if required else 'optional'} request body introduced on {label}",
                     [f"new.request_body.required={required}"])
            return
        if new is None:
            self.add(DiffItemKind.REQUEST_BODY_REMOVED, where, subject, PB, old, None,
                     f"request body removed from {label}", ["request_body.removed"])
            return
        if "ref" in old or "ref" in new:
            self.add(DiffItemKind.REQUEST_SCHEMA_CHANGED, where, subject, UNK, old, new,
                     "referenced request body changed (definition not in the normalized contract)",
                     ["request_body.ref.changed"])
            return
        if not old.get("required") and new.get("required"):
            self.add(DiffItemKind.REQUEST_BODY_BECAME_REQUIRED, f"{where}.required", subject, BR, False, True,
                     f"request body on {label} became required", ["new.request_body.required=true"])
        elif old.get("required") and not new.get("required"):
            self.add(DiffItemKind.REQUEST_BODY_BECAME_OPTIONAL, f"{where}.required", subject, NB, True, False,
                     f"request body on {label} became optional", ["new.request_body.required=false"])
        self._content(label, _Ctx.REQUEST, Direction.REQUEST, where, subject, old.get("content") or {},
                      new.get("content") or {}, DiffItemKind.REQUEST_MEDIA_TYPE_ADDED,
                      DiffItemKind.REQUEST_MEDIA_TYPE_REMOVED)
        self._body_field_renames(label, where, subject, old.get("content") or {}, new.get("content") or {})

    def _body_field_renames(
        self, label: str, where: str, subject: DiffSubject, old: Mapping[str, Any], new: Mapping[str, Any]
    ) -> None:
        """Hinted request-body field renames upgrade the removed/added pair
        the schema diff already emitted into one rename item."""

        for rename in self.hints.operation_parameter_renames:
            if rename.scope != label or rename.location != "body":
                continue
            removed = [
                i for i in self.items
                if i.kind is DiffItemKind.REQUEST_FIELD_REMOVED and i.location.startswith(where)
                and i.subject.member == rename.old
            ]
            added = [
                i for i in self.items
                if i.kind in (DiffItemKind.REQUEST_FIELD_ADDED_REQUIRED, DiffItemKind.REQUEST_FIELD_ADDED_OPTIONAL)
                and i.location.startswith(where) and i.subject.member == rename.new
            ]
            if not removed or not added:
                self.notes.append(f"hint body rename {rename.old}->{rename.new} on {label} not reflected in the diff")
                continue
            for item in removed + added:
                self.items.remove(item)
            self.add(DiffItemKind.REQUEST_FIELD_RENAMED, removed[0].location,
                     DiffSubject(SubjectKind.OPERATION, subject.method, subject.path, None, rename.old), BR,
                     removed[0].old, added[0].new, f"request body field '{rename.old}' renamed to '{rename.new}'",
                     [f"hint.body_field_rename={rename.old}->{rename.new}"], replacement=rename.new)

    def _content(
        self,
        label: str,
        ctx: _Ctx,
        direction: Direction,
        where: str,
        subject: DiffSubject,
        old: Mapping[str, Any],
        new: Mapping[str, Any],
        added_kind: DiffItemKind,
        removed_kind: DiffItemKind,
    ) -> None:
        for media in sorted(set(old) | set(new)):
            media_where = f"{where}.content[{media}]"
            if media not in old:
                self.add(added_kind, media_where, subject, NB, None, media, f"media type {media} added on {label}",
                         [f"new.media={media}"])
            elif media not in new:
                self.add(removed_kind, media_where, subject, BR, media, None, f"media type {media} removed on {label}",
                         [f"old.media={media}"])
            else:
                self.schema(old[media], new[media], ctx=ctx, direction=direction, location=media_where, subject=subject)

    def _responses(self, label: str, method: str, path: str, base: str, old: Mapping[str, Any],
                   new: Mapping[str, Any]) -> None:
        subject = DiffSubject(SubjectKind.OPERATION, method=method, path=path)
        for status in sorted(set(old) | set(new)):
            where = f"{base}.responses[{status}]"
            if status not in new:
                success = status.startswith("2")
                self.add(DiffItemKind.RESPONSE_STATUS_REMOVED, where, subject, BR if success else PB, old[status], None,
                         f"response {status} removed from {label}", [f"old.status={status}", f"success={success}"])
            elif status not in old:
                self.add(DiffItemKind.RESPONSE_STATUS_ADDED, where, subject, NB, None, new[status],
                         f"response {status} added to {label}", [f"new.status={status}"])
            else:
                old_resp, new_resp = old[status], new[status]
                if "ref" in old_resp or "ref" in new_resp:
                    if old_resp != new_resp:
                        self.add(DiffItemKind.RESPONSE_SCHEMA_CHANGED, where, subject, UNK, old_resp, new_resp,
                                 "referenced response changed (definition not in the normalized contract)",
                                 ["response.ref.changed"])
                    continue
                old_content = old_resp.get("content") if "content" in old_resp else {"*": old_resp.get("schema") or {}}
                new_content = new_resp.get("content") if "content" in new_resp else {"*": new_resp.get("schema") or {}}
                self._content(label, _Ctx.RESPONSE, Direction.RESPONSE, where, subject, old_content or {},
                              new_content or {}, DiffItemKind.RESPONSE_MEDIA_TYPE_ADDED,
                              DiffItemKind.RESPONSE_MEDIA_TYPE_REMOVED)

    # -- security -------------------------------------------------------

    def security(self, old: list[Any], new: list[Any], where: str, subject: DiffSubject, label: str) -> None:
        if old == new:
            return
        old_alternatives = [dict(r) for r in old]
        new_alternatives = [dict(r) for r in new]
        if not old_alternatives:
            self.add(DiffItemKind.AUTH_REQUIREMENT_ADDED, where, subject, BR, old, new,
                     f"authentication now required for {label}", ["old.security=none"])
            return
        if not new_alternatives:
            self.add(DiffItemKind.AUTH_REQUIREMENT_REMOVED, where, subject, NB, old, new,
                     f"authentication no longer required for {label}", ["new.security=none"])
            return

        def satisfied(have: Mapping[str, list[str]]) -> bool:
            return any(
                set(need) <= set(have) and all(set(need[s]) <= set(have[s]) for s in need)
                for need in new_alternatives
            )

        unsatisfied = [alt for alt in old_alternatives if not satisfied(alt)]
        if not unsatisfied:
            # Every credential set that worked still works.
            removed_scopes = sorted(
                {f"{s}:{scope}" for alt in old_alternatives for s, scopes in alt.items() for scope in scopes}
                - {f"{s}:{scope}" for alt in new_alternatives for s, scopes in alt.items() for scope in scopes}
            )
            if removed_scopes:
                self.add(DiffItemKind.AUTH_SCOPE_REMOVED, where, subject, NB, old, new,
                         f"fewer scopes required for {label}", [f"scope.removed={s}" for s in removed_scopes])
            return
        same_schemes = any(set(alt) == set(need) for alt in unsatisfied for need in new_alternatives)
        if same_schemes:
            added_scopes = sorted(
                {f"{s}:{scope}" for need in new_alternatives for s, scopes in need.items() for scope in scopes}
                - {f"{s}:{scope}" for alt in old_alternatives for s, scopes in alt.items() for scope in scopes}
            )
            self.add(DiffItemKind.AUTH_SCOPE_ADDED, where, subject, BR, old, new,
                     f"additional scopes/permissions required for {label}",
                     [f"scope.added={s}" for s in added_scopes] or ["scopes.changed"])
        else:
            old_schemes = sorted({s for alt in old_alternatives for s in alt})
            new_schemes = sorted({s for alt in new_alternatives for s in alt})
            self.add(DiffItemKind.AUTH_SCHEME_CHANGED, where, subject, BR, old, new,
                     f"authentication scheme for {label} changed from {', '.join(old_schemes)} to {', '.join(new_schemes)}",
                     [f"old.schemes={','.join(old_schemes)}", f"new.schemes={','.join(new_schemes)}"])

    def global_security(self) -> None:
        self.security(self.old.get("security") or [], self.new.get("security") or [], "security",
                      DiffSubject(SubjectKind.AUTH_GLOBAL), "all operations")

    def security_schemes(self) -> None:
        old: Mapping[str, Any] = self.old.get("security_schemes") or {}
        new: Mapping[str, Any] = self.new.get("security_schemes") or {}
        for name in sorted(set(old) | set(new)):
            where = f"security_schemes[{name}]"
            subject = DiffSubject(SubjectKind.SECURITY_SCHEME, name=name)
            if name not in new:
                used = bool(operations_using_scheme(self.old, name))
                self.add(DiffItemKind.SECURITY_SCHEME_REMOVED, where, subject, BR if used else NB, old[name], None,
                         f"security scheme '{name}' removed", [f"old.scheme.used={used}"])
            elif name not in old:
                self.add(DiffItemKind.SECURITY_SCHEME_ADDED, where, subject, NB, None, new[name],
                         f"security scheme '{name}' added", ["new.scheme"])
            elif old[name] != new[name]:
                changed = sorted(k for k in set(old[name]) | set(new[name]) if old[name].get(k) != new[name].get(k))
                self.add(DiffItemKind.SECURITY_SCHEME_CHANGED, where, subject, BR, old[name], new[name],
                         f"security scheme '{name}' changed ({', '.join(changed)})",
                         [f"scheme.changed={k}" for k in changed])

    # -- components -----------------------------------------------------

    def components(self) -> None:
        old: Mapping[str, Any] = self.old.get("components") or {}
        new: Mapping[str, Any] = self.new.get("components") or {}
        directions = component_directions(self.old)
        new_directions = component_directions(self.new)
        for name in sorted(set(old) | set(new)):
            where = f"components[{name}]"
            subject = DiffSubject(SubjectKind.SCHEMA, name=name)
            if name not in new:
                still_referenced = any(name in _refs(v) for v in (self.new.get("paths") or {}).values())
                self.add(DiffItemKind.SCHEMA_REMOVED, where, subject, UNK if still_referenced else PB, old[name], None,
                         f"schema '{name}' removed", [f"new.still_referenced={still_referenced}"])
            elif name not in old:
                self.add(DiffItemKind.SCHEMA_ADDED, where, subject, NB, None, new[name], f"schema '{name}' added",
                         ["new.schema"])
            else:
                direction = directions.get(name, Direction.BOTH)
                if new_directions.get(name, direction) != direction:
                    direction = Direction.BOTH
                self.schema(old[name], new[name], ctx=_Ctx.SCHEMA, direction=direction, location=where,
                            subject=subject)


@dataclass(frozen=True, slots=True)
class OpenAPIDiff:
    items: tuple[ContractDiffItem, ...]
    notes: tuple[str, ...]


def diff_openapi(
    old: Mapping[str, Any], new: Mapping[str, Any], *, hints: ChangeHints = EMPTY_HINTS
) -> OpenAPIDiff:
    """Both arguments are M5-normalized contracts (``normalize_spec``)."""

    builder = _Builder(old=old, new=new, hints=hints)
    builder.operations()
    builder.global_security()
    builder.security_schemes()
    builder.components()
    unique: dict[str, ContractDiffItem] = {}
    for item in builder.items:
        existing = unique.get(item.key)
        if existing is None or most_severe(existing.compatibility, item.compatibility) is not existing.compatibility:
            unique[item.key] = item
    ordered = tuple(sorted(unique.values(), key=lambda i: (i.location, i.kind.value)))
    return OpenAPIDiff(items=ordered, notes=tuple(sorted(set(builder.notes))))


__all__ = [
    "Direction",
    "OpenAPIDiff",
    "canonical",
    "component_directions",
    "diff_openapi",
    "operation_schema_refs",
    "operations_using_schema",
    "operations_using_scheme",
    "template_key",
]
