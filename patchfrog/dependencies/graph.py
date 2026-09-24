"""Repository external-dependency graph (M5.7).

Expressed with the *existing* repository-graph primitives
(:class:`~patchfrog.intelligence.graph.GraphNode` /
:class:`~patchfrog.intelligence.graph.RepositoryEdge`) rather than a second
graph framework:

    symbol-or-file --USES_EXTERNAL_DEPENDENCY--> external dependency
    external dependency --DEPENDENCY_HAS_CONTRACT--> contract fingerprint

Symbol nodes use the same ``(file_path, qualified_name)`` identity as
the code index, so M6 can walk from an upstream contract change to the
exact consuming symbols and on into the existing caller/callee graph.
"""

from __future__ import annotations

from typing import Any

from patchfrog.dependencies.domain import DependencyInventory
from patchfrog.intelligence.graph import EdgeKind, GraphNode, NodeKind, RepositoryEdge


def dependency_node(key: str) -> GraphNode:
    return GraphNode(kind=NodeKind.EXTERNAL_DEPENDENCY, file_path="", qualified_name=key)


def build_dependency_graph(inventory: DependencyInventory) -> tuple[RepositoryEdge, ...]:
    edges: set[RepositoryEdge] = set()
    for dependency in inventory.dependencies:
        target = dependency_node(dependency.key)
        for site in dependency.usage_sites:
            source = (
                GraphNode(kind=NodeKind.SYMBOL, file_path=site.file_path, qualified_name=site.symbol)
                if site.symbol
                else GraphNode(kind=NodeKind.FILE, file_path=site.file_path)
            )
            edges.add(
                RepositoryEdge(kind=EdgeKind.USES_EXTERNAL_DEPENDENCY, source=source, target=target,
                               reason=site.evidence_type.value)
            )
        if dependency.contract is not None:
            edges.add(
                RepositoryEdge(
                    kind=EdgeKind.DEPENDENCY_HAS_CONTRACT,
                    source=target,
                    target=GraphNode(
                        kind=NodeKind.EXTERNAL_CONTRACT, file_path="",
                        qualified_name=dependency.contract.fingerprint.value,
                    ),
                    reason=dependency.contract.source.type.value,
                )
            )
    return tuple(
        sorted(
            edges,
            key=lambda e: (e.kind.value, e.source.file_path, e.source.qualified_name or "",
                           e.target.qualified_name or "", e.reason or ""),
        )
    )


def inventory_tree(inventory: DependencyInventory) -> dict[str, Any]:
    """repository -> dependency -> usage sites -> contract/version, as a
    JSON-compatible tree (the M5.7 inventory view)."""

    return {
        "repository": inventory.repository,
        "commit_sha": inventory.commit_sha,
        "dependencies": [
            {
                "key": d.key,
                "contract": d.contract.fingerprint.value if d.contract else None,
                "version": d.version.display,
                "consumers": sorted({s.location for s in d.usage_sites}),
            }
            for d in inventory.dependencies
        ],
    }


__all__ = ["build_dependency_graph", "dependency_node", "inventory_tree"]
