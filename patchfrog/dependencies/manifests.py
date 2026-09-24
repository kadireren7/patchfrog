"""Package manifest and lockfile parsing -- declared ranges and resolved
versions, never anything else.

Supported: ``requirements*.txt``/``*.in``, ``pyproject.toml`` (PEP 621 and
Poetry), ``package.json``, ``go.mod`` (declarations); ``poetry.lock``,
``uv.lock``, ``Pipfile.lock``, ``package-lock.json``/``npm-shrinkwrap.json``,
``yarn.lock``, ``pnpm-lock.yaml`` (resolutions). Every parser is total:
a malformed file yields nothing rather than an exception.

A version *spec* that is really a URL (``git+https://user:token@host/...``)
is reduced to ``url:<host>`` -- credentials, paths and query strings in
dependency URLs are never retained.
"""

from __future__ import annotations

import json
import re
import tomllib
from collections.abc import Iterable
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import urlsplit

import yaml

from patchfrog.dependencies.domain import Ecosystem, PackageDeclaration, PackageResolution

_REQUIREMENTS_RE = re.compile(r"^requirements([._-][\w.-]+)?\.(txt|in)$", re.IGNORECASE)
_REQ_LINE_RE = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)\s*(\[[^\]]*\])?\s*([^;#]*)")
_PEP508_RE = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)\s*(\[[^\]]*\])?\s*(.*)$")
_YARN_HEADER_RE = re.compile(r'^"?((?:@[^/@\s"]+/)?[^@\s",]+)@')
_YARN_VERSION_RE = re.compile(r'^\s+version:?\s+"?([^"\s]+)"?')
_GO_REQUIRE_RE = re.compile(r"^\s*(?:require\s+)?([A-Za-z0-9][\w.\-/]*\.[a-z]{2,}[\w.\-/]*)\s+(v[\w.\-+]+)")


def normalize_package_name(ecosystem: Ecosystem, name: str) -> str:
    if ecosystem is Ecosystem.PYPI:
        return re.sub(r"[-_.]+", "-", name).lower()
    return name.strip()


def sanitize_spec(spec: str | None) -> str | None:
    """A version spec safe to store and print."""

    if spec is None:
        return None
    cleaned = spec.strip()
    if not cleaned:
        return None
    if "://" in cleaned or cleaned.startswith(("git+", "git@", "github:", "file:", "link:")):
        host = urlsplit(cleaned.split("+", 1)[-1]).hostname if "://" in cleaned else None
        return f"url:{host}" if host else "url"
    return cleaned[:128]


def is_manifest(path: str) -> bool:
    name = PurePosixPath(path).name
    return name in {"pyproject.toml", "package.json", "go.mod"} or bool(_REQUIREMENTS_RE.match(name))


def is_lockfile(path: str) -> bool:
    return PurePosixPath(path).name.lower() in {
        "poetry.lock", "uv.lock", "pipfile.lock", "package-lock.json", "npm-shrinkwrap.json",
        "yarn.lock", "pnpm-lock.yaml",
    }


def parse_manifest(path: str, text: str) -> list[PackageDeclaration]:
    name = PurePosixPath(path).name
    try:
        if _REQUIREMENTS_RE.match(name):
            return list(_requirements(path, text))
        if name == "pyproject.toml":
            return list(_pyproject(path, text))
        if name == "package.json":
            return list(_package_json(path, text))
        if name == "go.mod":
            return list(_go_mod(path, text))
    except (ValueError, TypeError, KeyError, AttributeError, tomllib.TOMLDecodeError, yaml.YAMLError):
        return []
    return []


def parse_lockfile(path: str, text: str) -> list[PackageResolution]:
    name = PurePosixPath(path).name.lower()
    try:
        if name in {"poetry.lock", "uv.lock"}:
            return list(_toml_lock(path, text))
        if name == "pipfile.lock":
            return list(_pipfile_lock(path, text))
        if name in {"package-lock.json", "npm-shrinkwrap.json"}:
            return list(_npm_lock(path, text))
        if name == "yarn.lock":
            return list(_yarn_lock(path, text))
        if name == "pnpm-lock.yaml":
            return list(_pnpm_lock(path, text))
    except (ValueError, TypeError, KeyError, AttributeError, tomllib.TOMLDecodeError, yaml.YAMLError):
        return []
    return []


def _requirements(path: str, text: str) -> Iterable[PackageDeclaration]:
    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith(("#", "-", "http:", "https:", "git+", "file:")):
            continue
        match = _REQ_LINE_RE.match(line)
        if match is None:
            continue
        yield PackageDeclaration(
            ecosystem=Ecosystem.PYPI,
            name=normalize_package_name(Ecosystem.PYPI, match.group(1)),
            spec=sanitize_spec(match.group(3)),
            manifest_path=path,
            line=number,
        )


def _pep508(path: str, requirement: str) -> PackageDeclaration | None:
    match = _PEP508_RE.match(requirement.split(";", 1)[0])
    if match is None:
        return None
    return PackageDeclaration(
        ecosystem=Ecosystem.PYPI,
        name=normalize_package_name(Ecosystem.PYPI, match.group(1)),
        spec=sanitize_spec(match.group(3).lstrip("@ ").strip() or None),
        manifest_path=path,
    )


def _pyproject(path: str, text: str) -> Iterable[PackageDeclaration]:
    data = tomllib.loads(text)
    project = data.get("project", {})
    requirements: list[str] = list(project.get("dependencies", []) or [])
    for extra in (project.get("optional-dependencies", {}) or {}).values():
        requirements.extend(extra or [])
    for requirement in requirements:
        if isinstance(requirement, str) and (declaration := _pep508(path, requirement)) is not None:
            yield declaration
    poetry = data.get("tool", {}).get("poetry", {})
    groups = [poetry.get("dependencies", {}) or {}, poetry.get("dev-dependencies", {}) or {}]
    groups += [g.get("dependencies", {}) or {} for g in (poetry.get("group", {}) or {}).values()]
    for group in groups:
        for package, spec in group.items():
            if package.lower() == "python":
                continue
            version = spec if isinstance(spec, str) else spec.get("version") if isinstance(spec, dict) else None
            yield PackageDeclaration(
                ecosystem=Ecosystem.PYPI,
                name=normalize_package_name(Ecosystem.PYPI, package),
                spec=sanitize_spec(version),
                manifest_path=path,
            )


def _package_json(path: str, text: str) -> Iterable[PackageDeclaration]:
    data: dict[str, Any] = json.loads(text)
    for section in ("dependencies", "devDependencies", "peerDependencies", "optionalDependencies"):
        for package, spec in (data.get(section) or {}).items():
            yield PackageDeclaration(
                ecosystem=Ecosystem.NPM,
                name=package,
                spec=sanitize_spec(spec if isinstance(spec, str) else None),
                manifest_path=path,
            )


def _go_mod(path: str, text: str) -> Iterable[PackageDeclaration]:
    for number, raw in enumerate(text.splitlines(), start=1):
        match = _GO_REQUIRE_RE.match(raw.split("//", 1)[0])
        if match is not None:
            yield PackageDeclaration(
                ecosystem=Ecosystem.GO, name=match.group(1), spec=match.group(2), manifest_path=path, line=number
            )


def _toml_lock(path: str, text: str) -> Iterable[PackageResolution]:
    for package in tomllib.loads(text).get("package", []) or []:
        if isinstance(package, dict) and package.get("name") and package.get("version"):
            yield PackageResolution(
                ecosystem=Ecosystem.PYPI,
                name=normalize_package_name(Ecosystem.PYPI, str(package["name"])),
                version=str(package["version"])[:64],
                lockfile_path=path,
            )


def _pipfile_lock(path: str, text: str) -> Iterable[PackageResolution]:
    data = json.loads(text)
    for section in ("default", "develop"):
        for package, info in (data.get(section) or {}).items():
            version = str(info.get("version", "")).lstrip("=") if isinstance(info, dict) else ""
            if version:
                yield PackageResolution(
                    ecosystem=Ecosystem.PYPI,
                    name=normalize_package_name(Ecosystem.PYPI, package),
                    version=version[:64],
                    lockfile_path=path,
                )


def _npm_lock(path: str, text: str) -> Iterable[PackageResolution]:
    data = json.loads(text)
    packages = data.get("packages") or {}
    seen: set[str] = set()
    for key, info in packages.items():
        if not key.startswith("node_modules/") or not isinstance(info, dict) or "version" not in info:
            continue
        name = key.rsplit("node_modules/", 1)[-1]
        if name in seen:
            continue
        seen.add(name)
        yield PackageResolution(Ecosystem.NPM, name, str(info["version"])[:64], path)
    for name, info in (data.get("dependencies") or {}).items():
        if name not in seen and isinstance(info, dict) and "version" in info:
            seen.add(name)
            yield PackageResolution(Ecosystem.NPM, name, str(info["version"])[:64], path)


def _yarn_lock(path: str, text: str) -> Iterable[PackageResolution]:
    current: str | None = None
    seen: set[str] = set()
    for raw in text.splitlines():
        if raw and not raw.startswith((" ", "#")):
            header = _YARN_HEADER_RE.match(raw)
            current = header.group(1) if header else None
            continue
        version = _YARN_VERSION_RE.match(raw)
        if current and version and current not in seen:
            seen.add(current)
            yield PackageResolution(Ecosystem.NPM, current, version.group(1)[:64], path)
            current = None


def _pnpm_lock(path: str, text: str) -> Iterable[PackageResolution]:
    data = yaml.safe_load(text) or {}
    seen: set[str] = set()
    for key in (data.get("packages") or {}):
        spec = str(key).lstrip("/")
        if "@" not in spec[1:]:
            continue
        name, _, version = spec.rpartition("@")
        version = version.split("(", 1)[0]
        if name and version and name not in seen:
            seen.add(name)
            yield PackageResolution(Ecosystem.NPM, name, version[:64], path)


__all__ = [
    "is_lockfile",
    "is_manifest",
    "normalize_package_name",
    "parse_lockfile",
    "parse_manifest",
    "sanitize_spec",
]
