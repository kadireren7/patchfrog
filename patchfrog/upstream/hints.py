"""Explicit change hints (``patchfrog_change_hints: 1``).

A contract diff can prove that ``chat.create`` disappeared and
``responses.create`` appeared, but not that one *replaced* the other. A
hints document is the only way a rename, move or replacement becomes
"known" -- PatchFrog never infers one from name similarity. Hints come
from a changelog, a migration guide, or an operator, and are recorded
(with their provenance line and a fingerprint) on every event they shape.

Example::

    patchfrog_change_hints: 1
    provenance: "acme-ai 2.0 migration guide"
    symbol_renames:
      - {old: chat.create, new: responses.create}
    parameter_renames:
      - {symbol: chat.create, old: prompt, new: input}
    operation_parameter_renames:
      - {operation: "POST /v1/sessions", in: query, old: expand, new: include}
    module_moves:
      - {old: acme_ai.legacy, new: acme_ai}
    endpoint_replacements:
      - {old: "GET /repos/{owner}/{repo}/legacy", new: "GET /repos/{owner}/{repo}/modern"}
    enum_replacements:
      - {symbol: chat.create, parameter: mode, old: fast, new: speed}
    return_field_renames:
      - {symbol: chat.create, old: text, new: output_text}
    required_parameter_values:
      - {symbol: responses.create, parameter: store, value: false}
      - {symbol: responses.create, parameter: model, from_parameter: engine}
    operation_symbols:
      - {operation: "POST /v1/checkout/sessions", symbol: checkout.sessions.create}

**Values are never invented and never secret.** ``value`` must be a
plain literal (string/number/boolean/null); a string that looks like a
credential is rejected outright.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

import yaml

HINTS_FORMAT_KEY = "patchfrog_change_hints"
MAX_PROVENANCE_CHARS = 200
MAX_HINT_ENTRIES = 500

_IDENT_CHAIN_RE = re.compile(r"^[A-Za-z_$][\w$]*(\.[A-Za-z_$][\w$]*)*$")
_MODULE_RE = re.compile(r"^[@A-Za-z_][\w@./-]*$")
_OPERATION_RE = re.compile(r"^(GET|PUT|POST|DELETE|OPTIONS|HEAD|PATCH|TRACE)\s+(/\S*)$", re.IGNORECASE)
_PARAM_IN = frozenset({"query", "header", "path", "cookie", "body"})
#: Credential-shaped strings are refused as hint values, whatever the key.
_SECRET_VALUE_RE = re.compile(
    r"(^(sk|pk|rk)[-_](live|test|proj)?[-_]?[A-Za-z0-9]{8,}|^gh[pousr]_[A-Za-z0-9]{16,}|^github_pat_|^xox[abpors]-"
    r"|^AKIA[0-9A-Z]{12,}|^AIza[0-9A-Za-z_-]{20,}|-----BEGIN|^eyJ[A-Za-z0-9_-]{10,}\.)"
)
_TOKENISH_RE = re.compile(r"^(?=.*\d)(?=.*[A-Za-z])[A-Za-z0-9_\-+/=]{24,}$")


class HintError(ValueError):
    """A hints document is malformed or contains a forbidden value."""


@dataclass(frozen=True, slots=True)
class Rename:
    old: str
    new: str


@dataclass(frozen=True, slots=True)
class ScopedRename:
    """A rename inside a scope: an SDK symbol, or an operation + location."""

    scope: str
    old: str
    new: str
    #: Operation parameter location (``query``/``header``/``path``/
    #: ``cookie``/``body``); ``None`` for SDK symbol scopes.
    location: str | None = None


@dataclass(frozen=True, slots=True)
class EnumReplacement:
    symbol: str
    parameter: str
    old: str
    new: str


@dataclass(frozen=True, slots=True)
class RequiredParameterValue:
    symbol: str
    parameter: str
    #: A JSON-compatible literal (str/int/float/bool/None) ...
    value: Any = None
    has_value: bool = False
    #: ... or the name of an existing argument at the call to copy.
    from_parameter: str | None = None


@dataclass(frozen=True, slots=True)
class OperationSymbol:
    method: str
    path: str
    symbol: str


@dataclass(frozen=True, slots=True)
class ChangeHints:
    provenance: str | None = None
    target: Mapping[str, str] = field(default_factory=dict)
    symbol_renames: tuple[Rename, ...] = ()
    parameter_renames: tuple[ScopedRename, ...] = ()
    operation_parameter_renames: tuple[ScopedRename, ...] = ()
    module_moves: tuple[Rename, ...] = ()
    #: ``old``/``new`` are ``"METHOD /path"`` strings, method upper-cased.
    endpoint_replacements: tuple[Rename, ...] = ()
    enum_replacements: tuple[EnumReplacement, ...] = ()
    return_field_renames: tuple[ScopedRename, ...] = ()
    required_parameter_values: tuple[RequiredParameterValue, ...] = ()
    operation_symbols: tuple[OperationSymbol, ...] = ()

    @property
    def empty(self) -> bool:
        return not any(
            (
                self.symbol_renames, self.parameter_renames, self.operation_parameter_renames, self.module_moves,
                self.endpoint_replacements, self.enum_replacements, self.return_field_renames,
                self.required_parameter_values, self.operation_symbols,
            )
        )

    # -- lookups used by the diff engines and the planner ------------------

    def symbol_rename(self, old: str) -> str | None:
        return next((r.new for r in self.symbol_renames if r.old == old), None)

    def parameter_rename(self, symbol: str, old: str) -> str | None:
        return next((r.new for r in self.parameter_renames if r.scope == symbol and r.old == old), None)

    def operation_parameter_rename(self, operation: str, location: str, old: str) -> str | None:
        return next(
            (
                r.new for r in self.operation_parameter_renames
                if r.scope == operation and r.location == location and r.old == old
            ),
            None,
        )

    def module_move(self, old: str) -> str | None:
        return next((r.new for r in self.module_moves if r.old == old), None)

    def endpoint_replacement(self, operation: str) -> str | None:
        return next((r.new for r in self.endpoint_replacements if r.old == operation), None)

    def enum_replacements_for(self, symbol: str, parameter: str) -> dict[str, str]:
        return {e.old: e.new for e in self.enum_replacements if e.symbol == symbol and e.parameter == parameter}

    def return_field_rename(self, symbol: str, old: str) -> str | None:
        return next((r.new for r in self.return_field_renames if r.scope == symbol and r.old == old), None)

    def required_value(self, symbol: str, parameter: str) -> RequiredParameterValue | None:
        return next(
            (v for v in self.required_parameter_values if v.symbol == symbol and v.parameter == parameter), None
        )

    def symbols_for_operation(self, method: str, path: str) -> tuple[str, ...]:
        return tuple(
            sorted({o.symbol for o in self.operation_symbols if o.method == method.lower() and o.path == path})
        )

    def fingerprint(self) -> str | None:
        if self.empty:
            return None
        return hashlib.sha256(json.dumps(self.canonical(), sort_keys=True, separators=(",", ":")).encode()).hexdigest()

    def canonical(self) -> dict[str, Any]:
        def renames(items: tuple[Rename, ...]) -> list[list[str]]:
            return sorted([r.old, r.new] for r in items)

        def scoped(items: tuple[ScopedRename, ...]) -> list[list[str]]:
            return sorted([r.scope, r.location or "", r.old, r.new] for r in items)

        return {
            "symbol_renames": renames(self.symbol_renames),
            "parameter_renames": scoped(self.parameter_renames),
            "operation_parameter_renames": scoped(self.operation_parameter_renames),
            "module_moves": renames(self.module_moves),
            "endpoint_replacements": renames(self.endpoint_replacements),
            "enum_replacements": sorted([e.symbol, e.parameter, e.old, e.new] for e in self.enum_replacements),
            "return_field_renames": scoped(self.return_field_renames),
            "required_parameter_values": sorted(
                [v.symbol, v.parameter, json.dumps(v.value) if v.has_value else "", v.from_parameter or ""]
                for v in self.required_parameter_values
            ),
            "operation_symbols": sorted([o.method, o.path, o.symbol] for o in self.operation_symbols),
        }


EMPTY_HINTS = ChangeHints()


def _entries(data: Mapping[str, Any], key: str) -> list[Mapping[str, Any]]:
    value = data.get(key) or []
    if not isinstance(value, list) or not all(isinstance(v, Mapping) for v in value):
        raise HintError(f"{key}: expected a list of mappings")
    if len(value) > MAX_HINT_ENTRIES:
        raise HintError(f"{key}: more than {MAX_HINT_ENTRIES} entries")
    return value


def _text(entry: Mapping[str, Any], key: str, section: str, pattern: re.Pattern[str] | None = None) -> str:
    value = entry.get(key)
    if not isinstance(value, str) or not value.strip():
        raise HintError(f"{section}: missing string field {key!r}")
    value = value.strip()
    if pattern is not None and not pattern.match(value):
        raise HintError(f"{section}: {key}={value!r} is not a valid identifier/path")
    return value


def parse_operation(value: str) -> tuple[str, str]:
    """``"GET /repos/{owner}"`` -> ``("get", "/repos/{owner}")``."""

    match = _OPERATION_RE.match(value.strip())
    if match is None:
        raise HintError(f"operation {value!r} must look like 'METHOD /path'")
    return match.group(1).lower(), match.group(2)


def operation_label(method: str, path: str) -> str:
    return f"{method.upper()} {path}"


def check_literal(value: Any, section: str) -> None:
    if value is None or isinstance(value, (bool, int, float)):
        return
    if not isinstance(value, str):
        raise HintError(f"{section}: value must be a string/number/boolean/null literal")
    if len(value) > 200 or "\n" in value:
        raise HintError(f"{section}: string value too long or multi-line")
    if _SECRET_VALUE_RE.search(value) or _TOKENISH_RE.match(value):
        raise HintError(f"{section}: value looks like a credential and is refused")


def parse_hints(data: Any) -> ChangeHints:
    if data is None:
        return EMPTY_HINTS
    if not isinstance(data, Mapping) or data.get(HINTS_FORMAT_KEY) != 1:
        raise HintError(f"not a hints document (expected '{HINTS_FORMAT_KEY}: 1')")
    provenance = data.get("provenance")
    target = data.get("target") or {}
    if not isinstance(target, Mapping):
        raise HintError("target: expected a mapping")

    def renames(key: str, pattern: re.Pattern[str]) -> tuple[Rename, ...]:
        return tuple(
            Rename(_text(e, "old", key, pattern), _text(e, "new", key, pattern)) for e in _entries(data, key)
        )

    endpoint_replacements = []
    for entry in _entries(data, "endpoint_replacements"):
        old_method, old_path = parse_operation(_text(entry, "old", "endpoint_replacements"))
        new_method, new_path = parse_operation(_text(entry, "new", "endpoint_replacements"))
        endpoint_replacements.append(
            Rename(operation_label(old_method, old_path), operation_label(new_method, new_path))
        )

    operation_parameter_renames = []
    for entry in _entries(data, "operation_parameter_renames"):
        method, path = parse_operation(_text(entry, "operation", "operation_parameter_renames"))
        location = _text(entry, "in", "operation_parameter_renames")
        if location not in _PARAM_IN:
            raise HintError(f"operation_parameter_renames: in={location!r} must be one of {sorted(_PARAM_IN)}")
        operation_parameter_renames.append(
            ScopedRename(
                operation_label(method, path),
                _text(entry, "old", "operation_parameter_renames"),
                _text(entry, "new", "operation_parameter_renames"),
                location=location,
            )
        )

    required_values = []
    for entry in _entries(data, "required_parameter_values"):
        section = "required_parameter_values"
        has_value = "value" in entry
        from_parameter = entry.get("from_parameter")
        if has_value == (from_parameter is not None):
            raise HintError(f"{section}: exactly one of 'value' or 'from_parameter' is required")
        if has_value:
            check_literal(entry["value"], section)
        elif not isinstance(from_parameter, str) or not _IDENT_CHAIN_RE.match(from_parameter):
            raise HintError(f"{section}: from_parameter must be an identifier")
        required_values.append(
            RequiredParameterValue(
                symbol=_text(entry, "symbol", section, _IDENT_CHAIN_RE),
                parameter=_text(entry, "parameter", section, _IDENT_CHAIN_RE),
                value=entry.get("value"),
                has_value=has_value,
                from_parameter=from_parameter if isinstance(from_parameter, str) else None,
            )
        )

    enum_replacements = []
    for entry in _entries(data, "enum_replacements"):
        section = "enum_replacements"
        old, new = entry.get("old"), entry.get("new")
        if not isinstance(old, str) or not isinstance(new, str):
            raise HintError(f"{section}: old/new must be strings")
        check_literal(new, section)
        enum_replacements.append(
            EnumReplacement(
                symbol=_text(entry, "symbol", section, _IDENT_CHAIN_RE),
                parameter=_text(entry, "parameter", section, _IDENT_CHAIN_RE),
                old=old,
                new=new,
            )
        )

    operation_symbols = []
    for entry in _entries(data, "operation_symbols"):
        method, path = parse_operation(_text(entry, "operation", "operation_symbols"))
        operation_symbols.append(
            OperationSymbol(method, path, _text(entry, "symbol", "operation_symbols", _IDENT_CHAIN_RE))
        )

    return ChangeHints(
        provenance=str(provenance)[:MAX_PROVENANCE_CHARS] if provenance else None,
        target={str(k): str(v) for k, v in target.items() if v is not None},
        symbol_renames=renames("symbol_renames", _IDENT_CHAIN_RE),
        parameter_renames=tuple(
            ScopedRename(
                _text(e, "symbol", "parameter_renames", _IDENT_CHAIN_RE),
                _text(e, "old", "parameter_renames", _IDENT_CHAIN_RE),
                _text(e, "new", "parameter_renames", _IDENT_CHAIN_RE),
            )
            for e in _entries(data, "parameter_renames")
        ),
        operation_parameter_renames=tuple(operation_parameter_renames),
        module_moves=renames("module_moves", _MODULE_RE),
        endpoint_replacements=tuple(endpoint_replacements),
        enum_replacements=tuple(enum_replacements),
        return_field_renames=tuple(
            ScopedRename(
                _text(e, "symbol", "return_field_renames", _IDENT_CHAIN_RE),
                _text(e, "old", "return_field_renames", _IDENT_CHAIN_RE),
                _text(e, "new", "return_field_renames", _IDENT_CHAIN_RE),
            )
            for e in _entries(data, "return_field_renames")
        ),
        required_parameter_values=tuple(required_values),
        operation_symbols=tuple(operation_symbols),
    )


def load_hints(text: str) -> ChangeHints:
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise HintError(f"hints are not valid YAML/JSON: {exc}") from exc
    return parse_hints(data)


__all__ = [
    "EMPTY_HINTS",
    "HINTS_FORMAT_KEY",
    "ChangeHints",
    "EnumReplacement",
    "HintError",
    "OperationSymbol",
    "Rename",
    "RequiredParameterValue",
    "ScopedRename",
    "check_literal",
    "load_hints",
    "operation_label",
    "parse_hints",
    "parse_operation",
]
