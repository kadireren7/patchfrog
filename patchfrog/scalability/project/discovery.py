"""Application discovery over one repository snapshot (Milestone 2.3).

Pure engine: the caller supplies the file list and a :class:`SourceReader`
that returns verified bytes (or ``None`` with a recorded reason). This module
never touches the filesystem itself, so the same function can run under the
local JobRunner today and under any other runner later.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Protocol

from patchfrog.scalability.project.domain import (
    DISCOVERY_SCHEMA_VERSION,
    ApplicationModel,
    Coverage,
    Limitation,
    LimitationCode,
    Location,
)
from patchfrog.scalability.project.fastapi_routes import DiscoveryLimits
from patchfrog.scalability.project.frameworks import (
    detect_frameworks,
    is_python_lockfile,
    is_python_manifest,
)
from patchfrog.scalability.project.sqlalchemy_models import discover_database


class SourceReader(Protocol):
    def read(self, path: str) -> bytes | None:
        """Verified bytes of ``path``, or None (the reason is in ``skipped``)."""

    @property
    def skipped(self) -> Mapping[str, str]:
        """path -> reason for every file that could not be read."""


def select_inputs(paths: Sequence[str]) -> tuple[list[str], list[str], list[str]]:
    """(python files, Python manifests, Python lockfiles), each sorted."""

    ordered = sorted(paths)
    return (
        [p for p in ordered if p.endswith(".py")],
        [p for p in ordered if is_python_manifest(p)],
        [p for p in ordered if is_python_lockfile(p)],
    )


def discover_application(
    *,
    revision_id: str,
    paths: Sequence[str],
    reader: SourceReader,
    limits: DiscoveryLimits,
    checkpoint: Callable[[], None] = lambda: None,
) -> ApplicationModel:
    python, manifest_paths, lockfile_paths = select_inputs(paths)
    limitations: list[Limitation] = []
    limit_reached = False
    if len(python) > limits.max_python_files:
        limit_reached = True
        limitations.append(
            Limitation(
                LimitationCode.LIMIT_REACHED,
                None,
                f"max_python_files: {len(python) - limits.max_python_files} of {len(python)} Python files not analysed",
            )
        )
        python = python[: limits.max_python_files]

    def text(path: str) -> str | None:
        checkpoint()
        data = reader.read(path)
        return None if data is None else data.decode("utf-8", errors="replace")

    manifests = {p: t for p in manifest_paths if (t := text(p)) is not None}
    lockfiles = {p: t for p in lockfile_paths if (t := text(p)) is not None}

    discovery = discover_database(python, reader.read, limits, checkpoint)
    routers, includes, routes, dependency_functions, unresolved, route_limitations = discovery.assemble()
    database = discovery.analyse(routes, dependency_functions)
    limitations.extend(route_limitations)
    limitations.extend(x for x in discovery.limitations_after_assembly())
    unresolved = tuple(
        sorted(
            set(unresolved) | set(discovery.unresolved_after_assembly()),
            key=lambda x: (x.location, x.kind.value, x.expression, x.reason),
        )
    )
    limitations.extend(
        Limitation(LimitationCode.FILE_SKIPPED, Location(path, 0), reason)
        for path, reason in sorted(reader.skipped.items())
    )
    limit_reached = limit_reached or discovery.limit_reached or any(
        reason.startswith("max_") for reason in reader.skipped.values()
    )
    limitations = sorted(set(limitations), key=lambda x: (x.code.value, x.location or Location("", 0), x.detail))
    reasons = sorted(
        {f"limitation:{x.code.value}" for x in limitations}
        | {f"unresolved:{u.kind.value}" for u in unresolved}
        | {f"db_unresolved:{u.kind.value}" for u in database.unresolved}
    )
    coverage = Coverage(
        python_files=len(python),
        scanned_files=discovery.scanned_files,
        parsed_files=discovery.parsed_files,
        complete=not reasons,
        incomplete_reasons=tuple(reasons),
        limit_reached=limit_reached,
    )
    return ApplicationModel(
        schema_version=DISCOVERY_SCHEMA_VERSION,
        revision_id=revision_id,
        frameworks=detect_frameworks(manifests, lockfiles, discovery.importing),
        routers=routers,
        includes=includes,
        routes=routes,
        dependency_functions=dependency_functions,
        unresolved=unresolved,
        limitations=tuple(limitations),
        coverage=coverage,
        database=database,
    )
