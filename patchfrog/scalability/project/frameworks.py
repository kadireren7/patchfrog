"""Framework and version evidence from dependency manifests and lockfiles
(Milestone 2.3). Reuses :mod:`patchfrog.dependencies.manifests`; nothing is
installed, resolved over the network, or executed (``setup.py`` is never
read: running it is the only way to learn what it declares).

Version rules (never invent an installed version):
* ``resolved_version`` comes only from lockfile evidence, and only when every
  lockfile that mentions the package names the same version;
* an exact pin in a manifest (``fastapi==0.110.0``) is still a *declaration*:
  it says what was requested, not what is installed;
* a package that is imported but neither declared nor locked is
  ``import_only`` with an unknown version.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from pathlib import PurePosixPath

from patchfrog.dependencies.domain import Ecosystem
from patchfrog.dependencies.manifests import (
    is_lockfile,
    is_manifest,
    parse_lockfile,
    parse_manifest,
)
from patchfrog.scalability.project.domain import (
    Declaration,
    FrameworkEvidence,
    Resolution,
    VersionStatus,
)

#: Packages whose evidence later milestones use. FastAPI is the analysed
#: framework; Starlette and Pydantic define its behaviour; SQLAlchemy and
#: SQLModel are the Phase 3 data-access targets.
TRACKED_PACKAGES: tuple[str, ...] = ("fastapi", "pydantic", "sqlalchemy", "sqlmodel", "starlette")
_PYTHON_LOCKFILES = frozenset({"poetry.lock", "uv.lock", "pipfile.lock"})
_PYTHON_MANIFEST_NAMES = frozenset({"pyproject.toml"})


def is_python_manifest(path: str) -> bool:
    name = PurePosixPath(path).name
    return is_manifest(path) and (name in _PYTHON_MANIFEST_NAMES or name.lower().startswith("requirements"))


def is_python_lockfile(path: str) -> bool:
    return is_lockfile(path) and PurePosixPath(path).name.lower() in _PYTHON_LOCKFILES


def detect_frameworks(
    manifests: Mapping[str, str], lockfiles: Mapping[str, str], importing_files: Mapping[str, int]
) -> tuple[FrameworkEvidence, ...]:
    """``importing_files`` maps a top-level import name to the number of
    analysed files importing it."""

    declarations: dict[str, set[Declaration]] = {}
    resolutions: dict[str, set[Resolution]] = {}
    for path in sorted(manifests):
        for item in parse_manifest(path, manifests[path]):
            if item.ecosystem is Ecosystem.PYPI and item.name in TRACKED_PACKAGES:
                declarations.setdefault(item.name, set()).add(Declaration(path, item.line, item.spec))
    for path in sorted(lockfiles):
        for resolved in parse_lockfile(path, lockfiles[path]):
            if resolved.ecosystem is Ecosystem.PYPI and resolved.name in TRACKED_PACKAGES:
                resolutions.setdefault(resolved.name, set()).add(Resolution(path, resolved.version))

    evidence = []
    for package in TRACKED_PACKAGES:
        declared = _sorted_declarations(declarations.get(package, ()))
        locked = tuple(sorted(resolutions.get(package, ()), key=lambda r: (r.lockfile_path, r.version)))
        imports = importing_files.get(package, 0)
        if not declared and not locked and not imports:
            continue
        versions = {r.version for r in locked}
        if len(versions) == 1:
            status, version = VersionStatus.RESOLVED, next(iter(versions))
        elif versions:
            status, version = VersionStatus.CONFLICTING, None
        elif declared:
            status, version = VersionStatus.DECLARED_UNRESOLVED, None
        else:
            status, version = VersionStatus.IMPORT_ONLY, None
        evidence.append(FrameworkEvidence(package, declared, locked, imports, status, version))
    return tuple(evidence)


def _sorted_declarations(items: Iterable[Declaration]) -> tuple[Declaration, ...]:
    return tuple(sorted(items, key=lambda d: (d.manifest_path, d.line or 0, d.spec or "")))
