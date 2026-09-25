"""Build :class:`ExternalChangeEvent`s from explicit, local evidence (M6.1).

Supported sources (no watchers, no network -- M11 will schedule/poll):

- two contract files (OpenAPI 3.x / Swagger 2.0, or two SDK surface
  documents) -- :func:`build_contract_change`;
- an old contract taken from the M5 registry's snapshot history plus a
  new contract file -- the same function with
  ``source=REGISTRY_SNAPSHOT``;
- a declared/resolved package version pair -- :func:`build_version_change`;
- GitHub release / changelog metadata (a local JSON document) --
  :func:`release_from_metadata`, attached to either of the above.

The event fingerprint covers the dependency identity, the source kind,
both contract fingerprints/versions, the hints fingerprint and
``UPSTREAM_CHANGE_VERSION`` -- never the observation time -- so
re-ingesting the same change is idempotent.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import yaml

from patchfrog.dependencies.domain import Ecosystem
from patchfrog.dependencies.files import MAX_OPENAPI_FILE_BYTES, is_secret_store_path
from patchfrog.dependencies.openapi import (
    fingerprint_normalized,
    load_spec,
    normalize_spec,
    server_hosts,
    spec_title,
)
from patchfrog.upstream.classify import classify
from patchfrog.upstream.domain import (
    MAX_DIFF_ITEMS,
    UPSTREAM_CHANGE_VERSION,
    ContractDiffItem,
    DependencyRelease,
    DependencyTarget,
    ExternalChangeEvent,
    ExternalChangeKind,
    ExternalChangeSource,
    ExternalContractRevision,
)
from patchfrog.upstream.hints import EMPTY_HINTS, ChangeHints
from patchfrog.upstream.openapi_diff import diff_openapi
from patchfrog.upstream.package_version import version_diff_items
from patchfrog.upstream.sdk_surface import (
    SURFACE_FORMAT_KEY,
    SdkSurface,
    diff_surfaces,
    parse_surface,
)


class ContractLoadError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class LoadedContract:
    #: ``openapi`` | ``sdk_surface``.
    format: str
    normalized: Mapping[str, Any]
    fingerprint: str
    version: str | None
    ref: str
    api_hosts: tuple[str, ...] = ()
    title: str | None = None
    surface: SdkSurface | None = None


def parse_contract_document(data: Any, *, ref: str) -> LoadedContract:
    if isinstance(data, Mapping) and SURFACE_FORMAT_KEY in data:
        surface = parse_surface(data)
        return LoadedContract(
            format="sdk_surface", normalized=surface.normalized, fingerprint=surface.fingerprint,
            version=surface.version, ref=ref, surface=surface, title=surface.package,
        )
    if isinstance(data, Mapping):
        version = str(data.get("openapi") or data.get("swagger") or "")
        if version.startswith(("2.", "3.")):
            normalized = normalize_spec(data)
            info = data.get("info")
            api_version = str(info.get("version"))[:64] if isinstance(info, Mapping) and info.get("version") else None
            return LoadedContract(
                format="openapi", normalized=normalized, fingerprint=fingerprint_normalized(normalized).value,
                version=api_version, ref=ref, api_hosts=server_hosts(data), title=spec_title(data),
            )
    raise ContractLoadError(f"{ref}: neither an OpenAPI 3.x/Swagger 2.0 document nor an SDK surface")


def load_contract_file(path: Path) -> LoadedContract:
    if is_secret_store_path(path.name):
        raise ContractLoadError(f"{path.name}: refusing to open a secret-store-like file")
    try:
        if path.stat().st_size > MAX_OPENAPI_FILE_BYTES:
            raise ContractLoadError(f"{path.name}: larger than {MAX_OPENAPI_FILE_BYTES} bytes")
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ContractLoadError(f"{path}: cannot read ({exc.__class__.__name__})") from exc
    spec = load_spec(text)
    if spec is not None:
        return parse_contract_document(spec, ref=path.name)
    try:
        data = json.loads(text) if text.lstrip().startswith("{") else yaml.safe_load(text)
    except (json.JSONDecodeError, yaml.YAMLError) as exc:
        raise ContractLoadError(f"{path.name}: not valid YAML/JSON") from exc
    return parse_contract_document(data, ref=path.name)


def contract_from_registry_snapshot(normalized_json: str, *, fingerprint: str, ref: str,
                                    version: str | None) -> LoadedContract:
    """The old side of a registry-backed diff: an M5 contract snapshot's
    stored normalized structure (never re-derived)."""

    normalized = json.loads(normalized_json)
    fmt = "sdk_surface" if normalized.get("format") == "sdk_surface" else "openapi"
    if fmt == "openapi" and "paths" not in normalized:
        raise ContractLoadError(f"{ref}: registry snapshot is not an OpenAPI contract (SDK usage snapshots record "
                                "consumed surface only and cannot be diffed structurally)")
    return LoadedContract(format=fmt, normalized=normalized, fingerprint=fingerprint, version=version, ref=ref)


# -- identity ------------------------------------------------------------------


def _merge_target(base: DependencyTarget, hints: ChangeHints, explicit: DependencyTarget | None) -> DependencyTarget:
    target = base
    hinted = hints.target
    if hinted:
        ecosystem = target.ecosystem
        if hinted.get("ecosystem"):
            try:
                ecosystem = Ecosystem(hinted["ecosystem"])
            except ValueError:
                ecosystem = target.ecosystem
        target = replace(
            target,
            provider_key=hinted.get("provider", target.provider_key),
            ecosystem=ecosystem,
            package_name=hinted.get("package", target.package_name),
            dependency_key=hinted.get("dependency_key", target.dependency_key),
            display_name=hinted.get("display_name", target.display_name),
        )
    if explicit is not None:
        target = replace(
            target,
            provider_key=explicit.provider_key or target.provider_key,
            ecosystem=explicit.ecosystem or target.ecosystem,
            package_name=explicit.package_name or target.package_name,
            dependency_key=explicit.dependency_key or target.dependency_key,
            api_hosts=explicit.api_hosts or target.api_hosts,
            modules=explicit.modules or target.modules,
            display_name=explicit.display_name or target.display_name,
        )
    return target


def event_fingerprint(
    *,
    target: DependencyTarget,
    source: ExternalChangeSource,
    old: ExternalContractRevision,
    new: ExternalContractRevision,
    hints_fingerprint: str | None,
) -> str:
    payload = {
        "engine": UPSTREAM_CHANGE_VERSION,
        "target": target.identity(),
        # A registry-snapshot old side and a file old side with the same
        # fingerprint are the same change.
        "source": "contract" if source in (
            ExternalChangeSource.OPENAPI_REVISION, ExternalChangeSource.SDK_SURFACE,
            ExternalChangeSource.MANUAL_CONTRACT_PAIR, ExternalChangeSource.REGISTRY_SNAPSHOT,
        ) else source.value,
        "old": [old.fingerprint, old.version],
        "new": [new.fingerprint, new.version],
        "hints": hints_fingerprint,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _finish(
    *,
    target: DependencyTarget,
    source: ExternalChangeSource,
    kind: ExternalChangeKind,
    source_ref: str,
    old: ExternalContractRevision,
    new: ExternalContractRevision,
    items: list[ContractDiffItem],
    notes: tuple[str, ...],
    hints: ChangeHints,
    release: DependencyRelease | None,
    observed_at: datetime | None,
) -> ExternalChangeEvent:
    truncated = len(items) > MAX_DIFF_ITEMS
    kept = tuple(items[:MAX_DIFF_ITEMS])
    hints_fingerprint = hints.fingerprint()
    return ExternalChangeEvent(
        target=target,
        source=source,
        kind=kind,
        source_ref=source_ref[:1024],
        old=old,
        new=new,
        diff=kept,
        classification=classify(kept, has_structural_evidence=kind is ExternalChangeKind.CONTRACT_REVISION),
        fingerprint=event_fingerprint(target=target, source=source, old=old, new=new,
                                      hints_fingerprint=hints_fingerprint),
        observed_at=observed_at or datetime.now(UTC),
        release=release,
        hints_fingerprint=hints_fingerprint,
        hints_provenance=hints.provenance,
        truncated=truncated,
        notes=notes + (("diff truncated",) if truncated else ()),
    )


def build_contract_change(
    old: LoadedContract,
    new: LoadedContract,
    *,
    hints: ChangeHints = EMPTY_HINTS,
    target: DependencyTarget | None = None,
    source: ExternalChangeSource | None = None,
    release: DependencyRelease | None = None,
    observed_at: datetime | None = None,
) -> ExternalChangeEvent:
    if old.format != new.format:
        raise ContractLoadError(f"cannot diff a {old.format} contract against a {new.format} contract")
    items: list[ContractDiffItem]
    if old.format == "openapi":
        diff = diff_openapi(old.normalized, new.normalized, hints=hints)
        items, notes = list(diff.items), diff.notes
        base = DependencyTarget(
            provider_key="openapi", api_hosts=tuple(sorted(set(old.api_hosts) | set(new.api_hosts))),
            display_name=new.title or old.title,
        )
        default_source = ExternalChangeSource.OPENAPI_REVISION
    else:
        assert old.surface is not None and new.surface is not None
        surface_diff = diff_surfaces(old.surface, new.surface, hints=hints)
        items, notes = list(surface_diff.items), surface_diff.notes
        items += version_diff_items(new.surface.package, old.version, new.version)
        base = DependencyTarget(
            provider_key=new.surface.package, ecosystem=new.surface.ecosystem, package_name=new.surface.package,
            modules=tuple(sorted(set(old.surface.modules) | set(new.surface.modules))), display_name=new.surface.package,
        )
        default_source = ExternalChangeSource.SDK_SURFACE
    return _finish(
        target=_merge_target(base, hints, target),
        source=source or default_source,
        kind=ExternalChangeKind.CONTRACT_REVISION,
        source_ref=f"{old.ref} -> {new.ref}",
        old=ExternalContractRevision(old.version, old.fingerprint, old.ref, old.normalized, old.format),
        new=ExternalContractRevision(new.version, new.fingerprint, new.ref, new.normalized, new.format),
        items=items,
        notes=notes,
        hints=hints,
        release=release,
        observed_at=observed_at,
    )


def build_version_change(
    target: DependencyTarget,
    old_version: str | None,
    new_version: str,
    *,
    hints: ChangeHints = EMPTY_HINTS,
    source: ExternalChangeSource = ExternalChangeSource.PACKAGE_VERSION,
    release: DependencyRelease | None = None,
    observed_at: datetime | None = None,
) -> ExternalChangeEvent:
    merged = _merge_target(target, hints, None)
    package = merged.package_name or merged.label
    items = list(version_diff_items(package, old_version, new_version))
    return _finish(
        target=merged,
        source=source,
        kind=ExternalChangeKind.VERSION_UPDATE,
        source_ref=f"{merged.label} {old_version or 'unknown'} -> {new_version}",
        old=ExternalContractRevision(old_version, None, None, None, "package"),
        new=ExternalContractRevision(new_version, None, None, None, "package"),
        items=items,
        notes=(),
        hints=hints,
        release=release,
        observed_at=observed_at,
    )


_TAG_VERSION_RE = re.compile(r"(\d+(?:\.\d+){0,2}(?:[.\-+]?[A-Za-z][\w.]*)?)")


def _sanitize_url(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        parts = urlsplit(value.strip())
    except ValueError:
        return None
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return None
    return urlunsplit((parts.scheme, parts.hostname, parts.path[:512], "", ""))


def release_from_metadata(data: Mapping[str, Any]) -> DependencyRelease:
    """A GitHub release object (``tag_name``/``published_at``/``html_url``/
    ``body``/``prerelease``) or a generic ``{version, tag, published_at,
    url, notes}`` mapping. Notes are digested, never stored."""

    tag = data.get("tag_name") or data.get("tag")
    version = data.get("version")
    if not version and isinstance(tag, str):
        match = _TAG_VERSION_RE.search(tag)
        version = match.group(1) if match else None
    if not isinstance(version, str) or not version:
        raise ContractLoadError("release metadata has no version or version-bearing tag")
    notes = data.get("body") or data.get("notes")
    return DependencyRelease(
        version=version[:64],
        tag=str(tag)[:128] if tag else None,
        published_at=str(data.get("published_at"))[:64] if data.get("published_at") else None,
        url=_sanitize_url(data.get("html_url") or data.get("url")),
        notes_sha256=hashlib.sha256(notes.encode()).hexdigest() if isinstance(notes, str) and notes else None,
        prerelease=bool(data.get("prerelease", False)),
    )


__all__ = [
    "ContractLoadError",
    "LoadedContract",
    "build_contract_change",
    "build_version_change",
    "contract_from_registry_snapshot",
    "event_fingerprint",
    "load_contract_file",
    "parse_contract_document",
    "release_from_metadata",
]
