"""SDK surface documents and their diff (M6.3).

Many SDKs have no machine-readable API contract. A *surface* document is
the smallest provider-agnostic description that still lets PatchFrog
prove a change structurally -- which call chains exist, which keyword
arguments they take (required or not, type, enum), what their results
expose, and which modules are importable::

    patchfrog_sdk_surface: 1
    package: acme-ai
    ecosystem: pypi
    version: "2.0.0"
    modules: [acme_ai]
    symbols:
      responses.create:
        params:
          input: {required: true, type: string}
          model: {required: true, type: string}
        returns:
          fields: {output_text: string, id: string}

It can come from SDK type stubs, generated metadata, a vendor changelog
or an operator-maintained compatibility fixture. Symbols are call chains
as PatchFrog's scanner records them (``chat.create`` for
``client.chat.create(...)``).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from patchfrog.dependencies.domain import Ecosystem
from patchfrog.dependencies.openapi import fingerprint_normalized
from patchfrog.upstream.domain import (
    CompatibilityClass,
    ContractDiffItem,
    DiffItemKind,
    DiffSubject,
    SubjectKind,
)
from patchfrog.upstream.hints import EMPTY_HINTS, ChangeHints
from patchfrog.upstream.openapi_diff import canonical

SURFACE_FORMAT_KEY = "patchfrog_sdk_surface"
MAX_SURFACE_SYMBOLS = 5000

NB = CompatibilityClass.NON_BREAKING
PB = CompatibilityClass.POTENTIALLY_BREAKING
BR = CompatibilityClass.BREAKING


class SurfaceError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class SdkSurface:
    package: str
    ecosystem: Ecosystem | None
    version: str | None
    #: The canonical structure fingerprints/diffs run over.
    normalized: Mapping[str, Any] = field(default_factory=dict)

    @property
    def modules(self) -> tuple[str, ...]:
        return tuple(self.normalized.get("modules") or ())

    @property
    def fingerprint(self) -> str:
        return fingerprint_normalized(self.normalized).value


def _param(value: Any, where: str) -> dict[str, Any]:
    if value is None:
        value = {}
    if not isinstance(value, Mapping):
        raise SurfaceError(f"{where}: parameter must be a mapping")
    out: dict[str, Any] = {"required": bool(value.get("required", False))}
    if value.get("type") is not None:
        out["type"] = str(value["type"])
    if isinstance(value.get("enum"), list):
        out["enum"] = sorted(str(v) for v in value["enum"])
    return out


def parse_surface(data: Any) -> SdkSurface:
    if not isinstance(data, Mapping) or data.get(SURFACE_FORMAT_KEY) != 1:
        raise SurfaceError(f"not an SDK surface document (expected '{SURFACE_FORMAT_KEY}: 1')")
    package = data.get("package")
    if not isinstance(package, str) or not package.strip():
        raise SurfaceError("package is required")
    ecosystem_value = data.get("ecosystem")
    try:
        ecosystem = Ecosystem(str(ecosystem_value)) if ecosystem_value else None
    except ValueError as exc:
        raise SurfaceError(f"unknown ecosystem {ecosystem_value!r}") from exc
    raw_symbols = data.get("symbols") or {}
    if not isinstance(raw_symbols, Mapping) or len(raw_symbols) > MAX_SURFACE_SYMBOLS:
        raise SurfaceError("symbols must be a mapping (bounded)")
    symbols: dict[str, Any] = {}
    for name, body in sorted(raw_symbols.items()):
        body = body or {}
        if not isinstance(body, Mapping):
            raise SurfaceError(f"symbol {name}: expected a mapping")
        params = body.get("params") or {}
        returns = body.get("returns") or {}
        if not isinstance(params, Mapping) or not isinstance(returns, Mapping):
            raise SurfaceError(f"symbol {name}: params/returns must be mappings")
        fields = returns.get("fields") or {}
        if not isinstance(fields, Mapping):
            raise SurfaceError(f"symbol {name}: returns.fields must be a mapping")
        symbols[str(name)] = {
            "params": {str(p): _param(v, f"{name}.{p}") for p, v in sorted(params.items())},
            "returns": {str(f): str(t) if t is not None else None for f, t in sorted(fields.items())},
        }
    modules = data.get("modules") or []
    if not isinstance(modules, list):
        raise SurfaceError("modules must be a list")
    version = data.get("version")
    normalized = {
        "format": "sdk_surface",
        "package": package.strip(),
        "ecosystem": ecosystem.value if ecosystem else None,
        "modules": sorted({str(m) for m in modules}),
        "symbols": symbols,
    }
    return SdkSurface(
        package=package.strip(), ecosystem=ecosystem, version=str(version) if version is not None else None,
        normalized=normalized,
    )


def _item(
    kind: DiffItemKind,
    location: str,
    subject: DiffSubject,
    compatibility: CompatibilityClass,
    old: Any,
    new: Any,
    explanation: str,
    evidence: tuple[str, ...],
    replacement: str | None = None,
) -> ContractDiffItem:
    return ContractDiffItem(kind, location, subject, compatibility, canonical(old), canonical(new), explanation,
                            tuple(sorted(evidence)), replacement)


@dataclass(frozen=True, slots=True)
class SurfaceDiff:
    items: tuple[ContractDiffItem, ...]
    notes: tuple[str, ...]


def diff_surfaces(old: SdkSurface, new: SdkSurface, *, hints: ChangeHints = EMPTY_HINTS) -> SurfaceDiff:
    items: list[ContractDiffItem] = []
    notes: list[str] = []
    old_symbols: Mapping[str, Any] = old.normalized.get("symbols") or {}
    new_symbols: Mapping[str, Any] = new.normalized.get("symbols") or {}
    rename_targets: set[str] = set()

    for name in sorted(old_symbols):
        subject = DiffSubject(SubjectKind.SDK_SYMBOL, name=name)
        location = f"symbols[{name}]"
        if name in new_symbols:
            items.extend(_symbol_pair(name, name, old_symbols[name], new_symbols[name], hints, notes))
            continue
        target = hints.symbol_rename(name)
        if target is not None and target in new_symbols:
            rename_targets.add(target)
            items.append(_item(DiffItemKind.SDK_SYMBOL_RENAMED, location, subject, BR, name, target,
                               f"{name} renamed to {target}", (f"hint.symbol_rename={name}->{target}",),
                               replacement=target))
            items.extend(_symbol_pair(name, target, old_symbols[name], new_symbols[target], hints, notes))
            continue
        if target is not None:
            notes.append(f"hint symbol_rename {name}->{target}: {target} is not in the new surface; ignored")
        items.append(_item(DiffItemKind.SDK_SYMBOL_REMOVED, location, subject, BR, old_symbols[name], None,
                           f"{name} removed with no known replacement", ("symbol.removed",)))

    for name in sorted(set(new_symbols) - set(old_symbols) - rename_targets):
        items.append(_item(DiffItemKind.SDK_SYMBOL_ADDED, f"symbols[{name}]",
                           DiffSubject(SubjectKind.SDK_SYMBOL, name=name), NB, None, new_symbols[name],
                           f"{name} added", ("symbol.added",)))

    old_modules = set(old.normalized.get("modules") or [])
    new_modules = set(new.normalized.get("modules") or [])
    for module in sorted(old_modules - new_modules):
        subject = DiffSubject(SubjectKind.MODULE, name=module)
        target = hints.module_move(module)
        if target is not None and target in new_modules:
            items.append(_item(DiffItemKind.SDK_MODULE_MOVED, f"modules[{module}]", subject, BR, module, target,
                               f"module {module} moved to {target}", (f"hint.module_move={module}->{target}",),
                               replacement=target))
        else:
            if target is not None:
                notes.append(f"hint module_move {module}->{target}: {target} is not in the new surface; ignored")
            items.append(_item(DiffItemKind.SDK_MODULE_REMOVED, f"modules[{module}]", subject, BR, module, None,
                               f"module {module} removed", ("module.removed",)))
    return SurfaceDiff(items=tuple(sorted(items, key=lambda i: (i.location, i.kind.value))),
                       notes=tuple(sorted(set(notes))))


def _symbol_pair(
    old_name: str,
    new_name: str,
    old: Mapping[str, Any],
    new: Mapping[str, Any],
    hints: ChangeHints,
    notes: list[str],
) -> list[ContractDiffItem]:
    items: list[ContractDiffItem] = []
    base = f"symbols[{old_name}]"
    old_params: Mapping[str, Any] = old.get("params") or {}
    new_params: Mapping[str, Any] = new.get("params") or {}
    renamed: dict[str, str] = {}
    for param in old_params:
        if param in new_params:
            continue
        target = hints.parameter_rename(old_name, param)
        if target is not None and target in new_params:
            renamed[param] = target
        elif target is not None:
            notes.append(f"hint parameter_rename {old_name}({param}->{target}): not in the new surface; ignored")

    for param in sorted(set(old_params) | set(new_params)):
        where = f"{base}.params[{param}]"
        subject = DiffSubject(SubjectKind.SDK_SYMBOL, name=old_name, member=param)
        if param in renamed:
            target = renamed[param]
            items.append(_item(DiffItemKind.SDK_PARAMETER_RENAMED, where, subject, BR, param, target,
                               f"{old_name}: argument '{param}' renamed to '{target}'",
                               (f"hint.parameter_rename={param}->{target}",), replacement=target))
            items.extend(_param_pair(old_name, param, old_params[param], new_params[target], where, subject, hints))
            continue
        if param in renamed.values():
            continue
        if param not in old_params:
            spec = new_params[param]
            if spec.get("required"):
                items.append(_item(DiffItemKind.SDK_PARAMETER_ADDED_REQUIRED, where, subject, BR, None, spec,
                                   f"{new_name}: new required argument '{param}'", ("new.required=true",)))
            else:
                items.append(_item(DiffItemKind.SDK_PARAMETER_ADDED_OPTIONAL, where, subject, NB, None, spec,
                                   f"{new_name}: new optional argument '{param}'", ("new.required=false",)))
        elif param not in new_params:
            items.append(_item(DiffItemKind.SDK_PARAMETER_REMOVED, where, subject, BR, old_params[param], None,
                               f"{old_name}: argument '{param}' removed; passing it will fail",
                               ("param.removed",)))
        else:
            items.extend(_param_pair(old_name, param, old_params[param], new_params[param], where, subject, hints))

    old_fields: Mapping[str, Any] = old.get("returns") or {}
    new_fields: Mapping[str, Any] = new.get("returns") or {}
    for name in sorted(set(old_fields) - set(new_fields)):
        where = f"{base}.returns[{name}]"
        subject = DiffSubject(SubjectKind.SDK_SYMBOL, name=old_name, member=f"returns.{name}")
        target = hints.return_field_rename(old_name, name)
        if target is not None and target in new_fields:
            items.append(_item(DiffItemKind.SDK_RETURN_FIELD_RENAMED, where, subject, BR, name, target,
                               f"{old_name}: result field '{name}' renamed to '{target}'",
                               (f"hint.return_field_rename={name}->{target}",), replacement=target))
        else:
            items.append(_item(DiffItemKind.SDK_RETURN_SHAPE_CHANGED, where, subject, BR, old_fields[name], None,
                               f"{old_name}: result field '{name}' removed", ("returns.field_removed",)))
    for name in sorted(set(old_fields) & set(new_fields)):
        if old_fields[name] != new_fields[name]:
            items.append(_item(DiffItemKind.SDK_RETURN_SHAPE_CHANGED, f"{base}.returns[{name}]",
                               DiffSubject(SubjectKind.SDK_SYMBOL, name=old_name, member=f"returns.{name}"), PB,
                               old_fields[name], new_fields[name],
                               f"{old_name}: result field '{name}' type changed",
                               (f"old.type={old_fields[name]}", f"new.type={new_fields[name]}")))
    return items


def _param_pair(
    symbol: str,
    param: str,
    old: Mapping[str, Any],
    new: Mapping[str, Any],
    where: str,
    subject: DiffSubject,
    hints: ChangeHints,
) -> list[ContractDiffItem]:
    items: list[ContractDiffItem] = []
    if not old.get("required") and new.get("required"):
        items.append(_item(DiffItemKind.SDK_PARAMETER_BECAME_REQUIRED, f"{where}.required", subject, BR, False, True,
                           f"{symbol}: argument '{param}' became required", ("new.required=true",)))
    elif old.get("required") and not new.get("required"):
        items.append(_item(DiffItemKind.SDK_PARAMETER_BECAME_OPTIONAL, f"{where}.required", subject, NB, True, False,
                           f"{symbol}: argument '{param}' became optional", ("new.required=false",)))
    if old.get("type") != new.get("type") and old.get("type") and new.get("type"):
        items.append(_item(DiffItemKind.SDK_PARAMETER_TYPE_CHANGED, f"{where}.type", subject, PB, old.get("type"),
                           new.get("type"), f"{symbol}: argument '{param}' type changed",
                           (f"old.type={old.get('type')}", f"new.type={new.get('type')}")))
    old_enum, new_enum = old.get("enum"), new.get("enum")
    if old_enum and new_enum:
        removed = sorted(set(old_enum) - set(new_enum))
        if removed:
            mapping = hints.enum_replacements_for(symbol, param)
            mapped = {v: mapping[v] for v in removed if v in mapping and mapping[v] in new_enum}
            for value, replacement in sorted(mapped.items()):
                items.append(_item(DiffItemKind.SDK_ENUM_VALUE_REPLACED, f"{where}.enum[{value}]",
                                   DiffSubject(SubjectKind.SDK_SYMBOL, name=symbol, member=param), BR, value,
                                   replacement, f"{symbol}: value '{value}' of '{param}' replaced by '{replacement}'",
                                   (f"hint.enum_replacement={value}->{replacement}",), replacement=replacement))
            unmapped = [v for v in removed if v not in mapped]
            if unmapped:
                items.append(_item(DiffItemKind.SDK_ENUM_NARROWED, f"{where}.enum", subject, BR, old_enum, new_enum,
                                   f"{symbol}: values removed from '{param}': {', '.join(unmapped)}",
                                   tuple(f"enum.removed={v}" for v in unmapped)))
    elif new_enum and not old_enum:
        items.append(_item(DiffItemKind.SDK_ENUM_NARROWED, f"{where}.enum", subject, BR, None, new_enum,
                           f"{symbol}: '{param}' now restricted to an enum", ("enum.introduced",)))
    return items


__all__ = ["SURFACE_FORMAT_KEY", "SdkSurface", "SurfaceDiff", "SurfaceError", "diff_surfaces", "parse_surface"]
