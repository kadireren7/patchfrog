"""The explicit, queryable repository graph.

Built once per indexing run from already-resolved calls/imports (see
:mod:`patchfrog.intelligence.resolution`) and symbol containment
(``parent_qualified_name``, already present on every parsed symbol) and
test heuristics (:mod:`patchfrog.intelligence.tests`). Nothing here
re-parses or touches Tree-sitter — the graph is queryable without any
parser involvement, matching the Phase 2 requirement directly.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from patchfrog.domain.code import ParsedFile
from patchfrog.intelligence.resolution import ResolutionStatus, ResolvedCall, ResolvedImport
from patchfrog.intelligence.tests import TestRelationship


class NodeKind(StrEnum):
    FILE = "file"
    SYMBOL = "symbol"
    #: M5 (patchfrog.dependencies.graph): an external API/SDK/contract the
    #: repository depends on. ``file_path`` is empty and ``qualified_name``
    #: is the dependency key (e.g. ``openai:pypi``) -- never persisted to
    #: ``repository_edges`` (the dependency registry owns its own tables).
    EXTERNAL_DEPENDENCY = "external_dependency"
    #: M5: one normalized contract snapshot; ``qualified_name`` is the
    #: contract fingerprint.
    EXTERNAL_CONTRACT = "external_contract"


class EdgeKind(StrEnum):
    FILE_IMPORTS_FILE = "file_imports_file"
    FILE_INCLUDES_FILE = "file_includes_file"
    SYMBOL_CONTAINS_SYMBOL = "symbol_contains_symbol"
    SYMBOL_CALLS_SYMBOL = "symbol_calls_symbol"
    SYMBOL_REFERENCES_SYMBOL = "symbol_references_symbol"
    FILE_TESTS_FILE = "file_tests_file"
    SYMBOL_TESTED_BY_SYMBOL = "symbol_tested_by_symbol"
    #: M5: a file/symbol uses an external dependency (a usage site).
    USES_EXTERNAL_DEPENDENCY = "uses_external_dependency"
    #: M5: an external dependency is described by a contract snapshot.
    DEPENDENCY_HAS_CONTRACT = "dependency_has_contract"


@dataclass(frozen=True, slots=True)
class GraphNode:
    """A reference to either a file or a (file, qualified_name) symbol."""

    kind: NodeKind
    file_path: str
    qualified_name: str | None = None


@dataclass(frozen=True, slots=True)
class RepositoryEdge:
    kind: EdgeKind
    source: GraphNode
    target: GraphNode
    reason: str | None = None


def build_graph(
    *,
    parsed_files: list[ParsedFile],
    resolved_imports: list[ResolvedImport],
    resolved_calls: list[ResolvedCall],
    test_relationships: list[TestRelationship],
) -> list[RepositoryEdge]:
    edges: list[RepositoryEdge] = []

    for parsed_file in parsed_files:
        for symbol in parsed_file.symbols:
            if symbol.parent_qualified_name is None:
                continue
            edges.append(
                RepositoryEdge(
                    kind=EdgeKind.SYMBOL_CONTAINS_SYMBOL,
                    source=GraphNode(NodeKind.SYMBOL, parsed_file.path, symbol.parent_qualified_name),
                    target=GraphNode(NodeKind.SYMBOL, parsed_file.path, symbol.qualified_name),
                )
            )

    for resolved_import in resolved_imports:
        if resolved_import.resolved_file_path is None:
            continue
        is_include = resolved_import.import_.raw_text.lstrip().startswith("#include")
        edges.append(
            RepositoryEdge(
                kind=EdgeKind.FILE_INCLUDES_FILE if is_include else EdgeKind.FILE_IMPORTS_FILE,
                source=GraphNode(NodeKind.FILE, resolved_import.file_path),
                target=GraphNode(NodeKind.FILE, resolved_import.resolved_file_path),
            )
        )

    for resolved_call in resolved_calls:
        if resolved_call.status is not ResolutionStatus.RESOLVED or resolved_call.resolved is None:
            continue
        if resolved_call.call.caller_qualified_name is None:
            continue  # no symbol to attribute the edge to (module-level call)
        edges.append(
            RepositoryEdge(
                kind=EdgeKind.SYMBOL_CALLS_SYMBOL,
                source=GraphNode(
                    NodeKind.SYMBOL, resolved_call.file_path, resolved_call.call.caller_qualified_name
                ),
                target=GraphNode(
                    NodeKind.SYMBOL, resolved_call.resolved.file_path, resolved_call.resolved.qualified_name
                ),
            )
        )

    for relationship in test_relationships:
        edges.append(
            RepositoryEdge(
                kind=EdgeKind.FILE_TESTS_FILE,
                source=GraphNode(NodeKind.FILE, relationship.test_file_path),
                target=GraphNode(NodeKind.FILE, relationship.source_file_path),
                reason=relationship.reason,
            )
        )

    return edges
