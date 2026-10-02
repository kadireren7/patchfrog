"""Organization compatibility graph (M10.1) -- a *derived view*.

``Workspace -> repositories -> dependency identities -> per-repository
dependency instances (version, usage sites, contract)``. It is computed from
M5 inventories/registry rows on demand and never stored: one dependency
identity is shared logically across repositories, while every per-repository
fact stays owned by that repository's own M5 rows (no duplicated truth).

Grouping is by the discovered provider identity, so a provider's Python SDK
consumer, a direct-REST consumer and a generated-OpenAPI-client consumer
land under the same provider identity.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from patchfrog.campaigns.domain import EnrolledRepository, Freshness
from patchfrog.campaigns.internal import InternalContract
from patchfrog.dependencies.domain import DependencyInventory, ExternalDependency
from patchfrog.upstream.consumers import DependencyIdentity, match_dependency


@dataclass(frozen=True, slots=True)
class DependencyInstance:
    """One repository's use of one dependency identity."""

    repository: str
    dependency_key: str
    kind: str
    ecosystem: str
    package_name: str | None
    version: str | None
    usage_sites: int
    usage_symbols: tuple[str, ...]
    contract_fingerprint: str | None
    confidence: str


@dataclass(frozen=True, slots=True)
class IdentityNode:
    identity_key: str
    display_name: str
    #: ``external`` | ``internal``
    origin: str
    producer_repository: str | None = None
    instances: tuple[DependencyInstance, ...] = ()

    @property
    def repositories(self) -> tuple[str, ...]:
        return tuple(sorted({i.repository for i in self.instances}))


@dataclass(frozen=True, slots=True)
class RepositoryNode:
    full_name: str
    freshness: Freshness
    has_inventory: bool
    commit_sha: str | None = None
    identities: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class OrgCompatibilityGraph:
    workspace_key: str
    repositories: tuple[RepositoryNode, ...]
    identities: tuple[IdentityNode, ...]
    notes: tuple[str, ...] = field(default_factory=tuple)

    def identity(self, identity_key: str) -> IdentityNode | None:
        return next((i for i in self.identities if i.identity_key == identity_key), None)

    def repositories_using(self, identity_key: str) -> tuple[str, ...]:
        node = self.identity(identity_key)
        return node.repositories if node else ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "workspace": self.workspace_key,
            "repositories": [
                {"name": r.full_name, "freshness": r.freshness.value, "has_inventory": r.has_inventory,
                 "commit_sha": r.commit_sha, "identities": list(r.identities)}
                for r in self.repositories
            ],
            "identities": [
                {"identity": i.identity_key, "display_name": i.display_name, "origin": i.origin,
                 "producer_repository": i.producer_repository,
                 "instances": [
                     {"repository": x.repository, "dependency_key": x.dependency_key, "kind": x.kind,
                      "ecosystem": x.ecosystem, "package": x.package_name, "version": x.version,
                      "usage_sites": x.usage_sites, "contract_fingerprint": x.contract_fingerprint,
                      "confidence": x.confidence}
                     for x in i.instances
                 ]}
                for i in self.identities
            ],
            "notes": list(self.notes),
        }


def _instance(repository: str, dependency: ExternalDependency) -> DependencyInstance:
    symbols = sorted({s.symbol for s in dependency.usage_sites if s.symbol})
    return DependencyInstance(
        repository=repository, dependency_key=dependency.key, kind=dependency.kind.value,
        ecosystem=dependency.ecosystem.value, package_name=dependency.package_name,
        version=dependency.version.display, usage_sites=len(dependency.usage_sites), usage_symbols=tuple(symbols[:20]),
        contract_fingerprint=dependency.contract.fingerprint.value if dependency.contract else None,
        confidence=dependency.confidence.value,
    )


def build_org_graph(
    workspace_key: str,
    entries: Sequence[tuple[EnrolledRepository, Freshness, DependencyInventory | None]],
    *,
    internal_contracts: Sequence[InternalContract] = (),
) -> OrgCompatibilityGraph:
    """``entries``: each enrolled repository, its freshness, and its latest
    inventory (``None`` when there is no usable discovery). Inventories of
    non-fresh repositories are still shown -- the node carries the freshness,
    so a reader sees the evidence *and* its age."""

    grouped: dict[str, list[DependencyInstance]] = {}
    names: dict[str, str] = {}
    origins: dict[str, tuple[str, str | None]] = {}
    repo_nodes: list[RepositoryNode] = []
    for repository, freshness, inventory in sorted(entries, key=lambda e: e[0].full_name):
        keys: set[str] = set()
        if inventory is not None:
            for dependency in inventory.dependencies:
                identity = DependencyIdentity.of(dependency)
                key, name, origin, producer = dependency.provider.key, dependency.provider.display_name, "external", None
                for contract in internal_contracts:
                    if match_dependency(contract.target, identity) is not None:
                        key, name = f"internal:{contract.contract_id}", contract.label
                        origin, producer = "internal", contract.producer_repository
                        break
                grouped.setdefault(key, []).append(_instance(repository.full_name, dependency))
                names.setdefault(key, name)
                origins.setdefault(key, (origin, producer))
                keys.add(key)
        repo_nodes.append(
            RepositoryNode(
                full_name=repository.full_name, freshness=freshness, has_inventory=inventory is not None,
                commit_sha=inventory.commit_sha if inventory else repository.last_discovery_commit_sha,
                identities=tuple(sorted(keys)),
            )
        )
    nodes = tuple(
        IdentityNode(
            identity_key=key, display_name=names[key], origin=origins[key][0], producer_repository=origins[key][1],
            instances=tuple(sorted(instances, key=lambda i: (i.repository, i.dependency_key))),
        )
        for key, instances in sorted(grouped.items())
    )
    notes = tuple(
        f"{r.full_name}: evidence is {r.freshness.value}" for r in repo_nodes if r.freshness is not Freshness.FRESH
    )
    return OrgCompatibilityGraph(workspace_key=workspace_key, repositories=tuple(repo_nodes), identities=nodes, notes=notes)


__all__ = [
    "DependencyInstance",
    "IdentityNode",
    "OrgCompatibilityGraph",
    "RepositoryNode",
    "build_org_graph",
]
