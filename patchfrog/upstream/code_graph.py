"""A local checkout's code graph, built with the *existing* primitives.

Exactly the pipeline :class:`patchfrog.indexing.service.IndexingService`
runs before it persists anything -- parser registry -> ``RepositoryResolver``
(imports, then calls) -> ``infer_test_relationships`` ->
``intelligence.graph.build_graph`` -- minus the database. Files are
enumerated with M5's safe walker (git-tracked files only in a checkout,
secret stores never opened, vendor/build directories skipped), so impact
analysis sees the same files dependency discovery saw.

Only languages the parser registry supports get call edges (Python,
C, C++ today). JS/TS usage sites still get enclosing symbols from M5's
lexical scanner, but no callers: :class:`LocalCodeGraph.languages`
records what was covered so blast radius can say so instead of
pretending the graph is complete.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from patchfrog.dependencies.files import WalkStats, iter_repository_files
from patchfrog.domain.code import ParsedFile
from patchfrog.indexing.inventory import is_test_path
from patchfrog.indexing.models import FileInventoryEntry
from patchfrog.intelligence.graph import EdgeKind, RepositoryEdge, build_graph
from patchfrog.intelligence.resolution import RepositoryResolver
from patchfrog.intelligence.tests import infer_test_relationships
from patchfrog.parsing.base import content_hash
from patchfrog.parsing.detect import detect_language
from patchfrog.parsing.registry import default_registry

MAX_GRAPH_FILE_BYTES = 1024 * 1024

SymbolRef = tuple[str, str]  # (file_path, qualified_name)


@dataclass
class LocalCodeGraph:
    edges: tuple[RepositoryEdge, ...] = ()
    callers: dict[SymbolRef, set[SymbolRef]] = field(default_factory=lambda: defaultdict(set))
    callees: dict[SymbolRef, set[SymbolRef]] = field(default_factory=lambda: defaultdict(set))
    #: source file -> {(test file, reason)}
    tests_for_file: dict[str, set[tuple[str, str]]] = field(default_factory=lambda: defaultdict(set))
    #: files that were parsed into the call graph
    graph_files: frozenset[str] = frozenset()
    languages: frozenset[str] = frozenset()
    test_files: frozenset[str] = frozenset()

    def callers_of(self, ref: SymbolRef) -> set[SymbolRef]:
        return set(self.callers.get(ref, set()))

    def callees_of(self, ref: SymbolRef) -> set[SymbolRef]:
        return set(self.callees.get(ref, set()))

    def has_call_graph(self, file_path: str) -> bool:
        return file_path in self.graph_files


EMPTY_GRAPH = LocalCodeGraph()


def build_local_code_graph(root: Path) -> LocalCodeGraph:
    registry = default_registry()
    inventory: list[FileInventoryEntry] = []
    parsed: list[ParsedFile] = []
    test_files: set[str] = set()
    for file in iter_repository_files(root, WalkStats()):
        path = file.relative_path
        if is_test_path(path):
            test_files.add(path)
        try:
            if file.absolute_path.stat().st_size > MAX_GRAPH_FILE_BYTES:
                continue
            data = file.absolute_path.read_bytes()
        except OSError:
            continue
        language = detect_language(relative_path=path, content=data)
        inventory.append(
            FileInventoryEntry(
                relative_path=path, language=language, size_bytes=len(data), content_hash=content_hash(data),
                git_blob_sha=None, is_test=is_test_path(path), is_generated=False,
            )
        )
        parser = registry.get(language) if language is not None else None
        if parser is None:
            continue
        try:
            parsed.append(parser.parse_file(relative_path=path, content=data))
        except Exception:  # a file the parser cannot handle is simply not in the graph
            continue

    resolver = RepositoryResolver(parsed)
    resolved_imports = resolver.resolve_imports()
    resolved_calls = resolver.resolve_calls()
    edges = build_graph(
        parsed_files=parsed,
        resolved_imports=resolved_imports,
        resolved_calls=resolved_calls,
        test_relationships=infer_test_relationships(inventory, resolved_imports),
    )
    graph = LocalCodeGraph(
        edges=tuple(edges),
        graph_files=frozenset(p.path for p in parsed),
        languages=frozenset(p.language.value for p in parsed),
        test_files=frozenset(test_files),
    )
    for edge in edges:
        if edge.kind is EdgeKind.SYMBOL_CALLS_SYMBOL and edge.source.qualified_name and edge.target.qualified_name:
            source = (edge.source.file_path, edge.source.qualified_name)
            target = (edge.target.file_path, edge.target.qualified_name)
            if source != target:
                graph.callers[target].add(source)
                graph.callees[source].add(target)
        elif edge.kind is EdgeKind.FILE_TESTS_FILE:
            graph.tests_for_file[edge.target.file_path].add((edge.source.file_path, edge.reason or "test"))
    return graph


__all__ = ["EMPTY_GRAPH", "LocalCodeGraph", "SymbolRef", "build_local_code_graph"]
