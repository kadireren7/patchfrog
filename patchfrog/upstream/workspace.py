"""Repository and multi-repository impact (M6.5-M6.7).

``analyze_repository`` = M5 discovery (the same inventory the registry
stores) -> dependency matching -> consumer mapping -> blast radius over
the checkout's code graph. ``analyze_workspace`` runs it over several
local checkouts (an organisation's repositories) and sorts them into:

- **affected** -- at least one DIRECT consumer;
- **uncertain** -- the dependency is used, but only POTENTIAL evidence
  reaches it (e.g. a major version bump without structural proof);
- **unaffected** -- the dependency is not used, or no usage reaches the
  changed surface, or the repository is already on the target version.

``registry_impact`` does the same from M5 registry rows alone (no
checkout): usage sites and symbols come from the registry, so consumer
mapping works but call arguments cannot be inspected and there is no
code graph -- results say so via lower confidence and coverage notes.

Relationships are never inferred from names, organisations or
similarity: a repository is related to a change only through its own
discovered dependency evidence.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from patchfrog.dependencies.adapters import (
    KNOWN_PROVIDER_SPECS,
    DependencyProviderAdapter,
    KnownProviderAdapter,
    ProviderSpec,
    default_provider_adapters,
)
from patchfrog.dependencies.discovery import discover_dependencies
from patchfrog.dependencies.domain import (
    DependencyInventory,
    DependencyUsageSite,
    Ecosystem,
)
from patchfrog.upstream.blast_radius import BlastRadius, compute_blast_radius
from patchfrog.upstream.code_graph import EMPTY_GRAPH, LocalCodeGraph, build_local_code_graph
from patchfrog.upstream.consumers import (
    ConsumerImpact,
    DependencyIdentity,
    map_consumers,
    match_dependency,
    no_call_locator,
    source_call_locator,
)
from patchfrog.upstream.domain import (
    DependencyTarget,
    ExternalChangeEvent,
    ExternalChangeKind,
    normalize_package_name,
)
from patchfrog.upstream.hints import EMPTY_HINTS, ChangeHints
from patchfrog.upstream.package_version import parse_version


class RepositoryImpactStatus(StrEnum):
    AFFECTED = "affected"
    UNCERTAIN = "uncertain"
    UNAFFECTED = "unaffected"


@dataclass(frozen=True, slots=True)
class DependencyMatch:
    identity: DependencyIdentity
    reason: str


@dataclass(frozen=True, slots=True)
class RepositoryImpact:
    repository: str
    commit_sha: str | None
    status: RepositoryImpactStatus
    reason: str
    matches: tuple[DependencyMatch, ...]
    consumer_impacts: tuple[ConsumerImpact, ...]
    blast_radii: tuple[BlastRadius, ...]
    notes: tuple[str, ...] = ()

    @property
    def direct_count(self) -> int:
        return sum(len(i.direct) for i in self.consumer_impacts)

    @property
    def potential_count(self) -> int:
        return sum(len(i.potential) for i in self.consumer_impacts)


@dataclass(frozen=True, slots=True)
class WorkspaceImpact:
    repositories: tuple[RepositoryImpact, ...]

    def _with(self, status: RepositoryImpactStatus) -> tuple[RepositoryImpact, ...]:
        return tuple(r for r in self.repositories if r.status is status)

    @property
    def affected(self) -> tuple[RepositoryImpact, ...]:
        return self._with(RepositoryImpactStatus.AFFECTED)

    @property
    def uncertain(self) -> tuple[RepositoryImpact, ...]:
        return self._with(RepositoryImpactStatus.UNCERTAIN)

    @property
    def unaffected(self) -> tuple[RepositoryImpact, ...]:
        return self._with(RepositoryImpactStatus.UNAFFECTED)


def adapters_for_target(target: DependencyTarget) -> list[DependencyProviderAdapter] | None:
    """Discovery adapters for a change target. An SDK described only by a
    surface document (modules PatchFrog has no built-in adapter for) gets a
    declarative adapter so its call chains become usage sites; otherwise
    the defaults (``None``)."""

    if not target.modules:
        return None
    known_modules = {m for spec in KNOWN_PROVIDER_SPECS for m in (*spec.python_modules, *spec.js_modules)}
    modules = tuple(m for m in target.modules if m.split(".")[0] not in known_modules)
    if not modules:
        return None
    python = target.ecosystem in (None, Ecosystem.PYPI)
    javascript = target.ecosystem in (None, Ecosystem.NPM)
    packages = (
        {target.ecosystem: (normalize_package_name(target.package_name),)}
        if target.ecosystem is not None and target.package_name else {}
    )
    spec = ProviderSpec(
        key=target.provider_key or normalize_package_name(target.package_name or modules[0]),
        display_name=target.display_name or target.package_name or modules[0],
        packages=packages,
        python_modules=tuple(sorted({m.split(".")[0] for m in modules})) if python else (),
        js_modules=modules if javascript else (),
    )
    return [*default_provider_adapters(), KnownProviderAdapter(spec)]


def _already_at_target(event: ExternalChangeEvent, identity: DependencyIdentity) -> bool:
    if event.kind is not ExternalChangeKind.VERSION_UPDATE:
        return False
    current, target = parse_version(identity.current_version), parse_version(event.new.version)
    return current is not None and target is not None and current == target


def _status(impacts: Sequence[ConsumerImpact], matches: Sequence[DependencyMatch],
            already: bool) -> tuple[RepositoryImpactStatus, str]:
    if not matches:
        return RepositoryImpactStatus.UNAFFECTED, "dependency not used"
    if already:
        return RepositoryImpactStatus.UNAFFECTED, "already on the target version"
    if any(i.direct for i in impacts):
        return RepositoryImpactStatus.AFFECTED, "usage reaches the changed contract"
    if any(i.potential for i in impacts):
        return RepositoryImpactStatus.UNCERTAIN, "dependency used; only version-level/indirect evidence"
    return RepositoryImpactStatus.UNAFFECTED, "dependency used; no usage reaches the changed surface"


def analyze_inventory(
    inventory: DependencyInventory,
    event: ExternalChangeEvent,
    *,
    hints: ChangeHints = EMPTY_HINTS,
    root: Path | None = None,
    graph: LocalCodeGraph | None = None,
) -> RepositoryImpact:
    matches: list[DependencyMatch] = []
    impacts: list[ConsumerImpact] = []
    radii: list[BlastRadius] = []
    already = False
    locate = source_call_locator(root) if root is not None else no_call_locator
    for dependency in inventory.dependencies:
        identity = DependencyIdentity.of(dependency)
        reason = match_dependency(event.target, identity, old_contract_fingerprint=event.old.fingerprint)
        if reason is None:
            continue
        matches.append(DependencyMatch(identity, reason))
        if _already_at_target(event, identity):
            already = True
            continue
        impact = map_consumers(
            event, repository=inventory.repository, dependency_key=dependency.key, match_reason=reason,
            usage_sites=dependency.usage_sites, hints=hints, locate=locate,
        )
        impacts.append(impact)
    if impacts and graph is None:
        graph = build_local_code_graph(root) if root is not None else EMPTY_GRAPH
    for impact in impacts:
        radii.append(compute_blast_radius(impact, graph or EMPTY_GRAPH))
    status, reason = _status(impacts, matches, already)
    notes = tuple(sorted({note for i in impacts for note in i.notes}))
    return RepositoryImpact(
        repository=inventory.repository,
        commit_sha=inventory.commit_sha,
        status=status,
        reason=reason,
        matches=tuple(matches),
        consumer_impacts=tuple(impacts),
        blast_radii=tuple(radii),
        notes=notes,
    )


def analyze_repository(
    root: Path,
    event: ExternalChangeEvent,
    *,
    hints: ChangeHints = EMPTY_HINTS,
    repository: str | None = None,
    commit_sha: str | None = None,
) -> tuple[RepositoryImpact, DependencyInventory]:
    inventory = discover_dependencies(
        root, repository=repository, commit_sha=commit_sha, adapters=adapters_for_target(event.target)
    )
    return analyze_inventory(inventory, event, hints=hints, root=root), inventory


def analyze_workspace(
    roots: Sequence[tuple[Path, str | None]],
    event: ExternalChangeEvent,
    *,
    hints: ChangeHints = EMPTY_HINTS,
) -> WorkspaceImpact:
    """``roots``: (checkout path, repository name or ``None``)."""

    results = [analyze_repository(root, event, hints=hints, repository=name)[0] for root, name in roots]
    return WorkspaceImpact(repositories=tuple(sorted(results, key=lambda r: r.repository)))


def current_version(inventory: DependencyInventory, target: DependencyTarget) -> str | None:
    """The version a repository currently uses for ``target`` (resolved
    lockfile version first, then the declared spec's version) -- the
    "from" side of a version-change event built with discovery/registry
    context instead of a hand-typed version."""

    for dependency in inventory.dependencies:
        if match_dependency(target, DependencyIdentity.of(dependency)) is None:
            continue
        for candidate in (dependency.version.resolved, dependency.version.declared):
            parsed = parse_version(candidate)
            if parsed is not None:
                return candidate if dependency.version.resolved == candidate else parsed.text
    return None


@dataclass(frozen=True, slots=True)
class RegistryDependency:
    """One M5 registry row plus its usage-site rows, as plain data."""

    repository: str
    commit_sha: str | None
    identity: DependencyIdentity
    usage_sites: tuple[DependencyUsageSite, ...] = field(default_factory=tuple)


def registry_impact(
    rows: Sequence[RegistryDependency],
    event: ExternalChangeEvent,
    *,
    hints: ChangeHints = EMPTY_HINTS,
    repositories: Sequence[str] = (),
) -> WorkspaceImpact:
    """Cross-repository impact from registry evidence only. ``repositories``
    lists every repository considered, so ones without the dependency are
    reported as unaffected rather than silently absent."""

    by_repo: dict[str, list[RegistryDependency]] = {name: [] for name in repositories}
    commits: dict[str, str | None] = {}
    for row in rows:
        by_repo.setdefault(row.repository, []).append(row)
        commits[row.repository] = row.commit_sha
    results: list[RepositoryImpact] = []
    for repository, deps in sorted(by_repo.items()):
        matches: list[DependencyMatch] = []
        impacts: list[ConsumerImpact] = []
        already = False
        for row in deps:
            reason = match_dependency(event.target, row.identity, old_contract_fingerprint=event.old.fingerprint)
            if reason is None:
                continue
            matches.append(DependencyMatch(row.identity, reason))
            if _already_at_target(event, row.identity):
                already = True
                continue
            impacts.append(
                map_consumers(event, repository=repository, dependency_key=row.identity.key, match_reason=reason,
                              usage_sites=row.usage_sites, hints=hints)
            )
        status, reason = _status(impacts, matches, already)
        results.append(
            RepositoryImpact(
                repository=repository,
                commit_sha=commits.get(repository),
                status=status,
                reason=reason,
                matches=tuple(matches),
                consumer_impacts=tuple(impacts),
                blast_radii=tuple(compute_blast_radius(i) for i in impacts),
                notes=("registry evidence only: call arguments not inspected, no code graph",) if impacts else (),
            )
        )
    return WorkspaceImpact(repositories=tuple(results))


__all__ = [
    "DependencyMatch",
    "RegistryDependency",
    "RepositoryImpact",
    "RepositoryImpactStatus",
    "WorkspaceImpact",
    "adapters_for_target",
    "analyze_inventory",
    "analyze_repository",
    "analyze_workspace",
    "current_version",
    "registry_impact",
]
