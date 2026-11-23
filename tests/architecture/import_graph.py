"""Static import-graph analysis for the architecture tests (Milestone 1.2).

Standard-library ``ast`` only: nothing under analysis is ever imported or
executed. Results are deterministic: files are discovered and every
collection is emitted in sorted order, so filesystem traversal order,
import ordering inside a file, and source formatting never change the
outcome.

Import kinds
------------
* ``module`` -- executed when the importing module is imported: top-level
  statements, class bodies, and top-level ``if``/``try``/``with`` blocks.
  Conditional top-level imports are treated as ``module`` (conservative:
  the branch may run).
* ``lazy`` -- inside a function or method body (including nested
  functions); executed only when that function runs.
* ``type_checking`` -- inside ``if TYPE_CHECKING:`` / ``if
  typing.TYPE_CHECKING:`` (anywhere). Never executed at runtime, but still
  a static coupling. Takes precedence over ``lazy``.

Resolution
----------
* Relative imports resolve against the importer's package (``__init__``
  files are their own package).
* ``from X import n`` depends on ``X.n`` when that is a module of the
  analysed tree, otherwise on ``X`` (``n`` is an attribute of ``X``).
* ``import a.b.c`` depends on ``a.b.c`` (or its deepest existing prefix).
* Importing a module executes every parent package ``__init__``; the
  closure computation (:func:`import_closure`) adds those explicitly.
* Dynamic imports (``importlib.import_module``, ``__import__``) and plain
  strings are invisible by construction. There are none in production code
  today (verified 2026-11-22); a dynamic import added later is a review
  concern this static check cannot see.
* ``from __future__`` imports are ignored.
"""

from __future__ import annotations

import ast
import json
import sys
from collections import defaultdict, deque
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date
from enum import StrEnum
from functools import cache, lru_cache
from pathlib import Path
from typing import TypedDict, cast

REPO_ROOT = Path(__file__).resolve().parents[2]
ROOT_PACKAGES = ("patchfrog", "apps")
BASELINE_PATH = Path(__file__).with_name("dependency_baseline.json")


class ImportKind(StrEnum):
    MODULE = "module"
    LAZY = "lazy"
    TYPE_CHECKING = "type_checking"


ALL_KINDS = frozenset(ImportKind)


@dataclass(frozen=True, order=True)
class ImportEdge:
    importer: str
    target: str
    kind: ImportKind
    lineno: int
    function: str | None
    internal: bool

    def where(self, graph: ModuleGraph) -> str:
        path = graph.paths.get(self.importer)
        location = f"{path}:{self.lineno}" if path else f"{self.importer}:{self.lineno}"
        scope = f" in {self.function}()" if self.function else ""
        return f"{location}{scope} [{self.kind.value}] {self.importer} -> {self.target}"


@dataclass(frozen=True)
class ModuleGraph:
    #: module name -> repo-relative POSIX path
    paths: Mapping[str, str]
    packages: frozenset[str]
    edges: tuple[ImportEdge, ...]
    by_importer: Mapping[str, tuple[ImportEdge, ...]] = field(default_factory=dict)


# --- discovery and parsing ---------------------------------------------------


def _module_name(relative: Path) -> tuple[str, bool]:
    parts = list(relative.with_suffix("").parts)
    is_package = parts[-1] == "__init__"
    if is_package:
        parts.pop()
    return ".".join(parts), is_package


def discover_modules(root: Path) -> dict[str, tuple[str, bool]]:
    """``{module: (relative posix path, is_package)}`` for every analysed file."""

    found: dict[str, tuple[str, bool]] = {}
    for package in ROOT_PACKAGES:
        base = root / package
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            relative = path.relative_to(root)
            name, is_package = _module_name(relative)
            found[name] = (relative.as_posix(), is_package)
    return found


@cache
def _parse(source: str, filename: str) -> ast.Module:
    return ast.parse(source, filename=filename)


def _is_type_checking_test(test: ast.expr) -> bool:
    if isinstance(test, ast.Name):
        return test.id == "TYPE_CHECKING"
    if isinstance(test, ast.Attribute):
        return test.attr == "TYPE_CHECKING" and isinstance(test.value, ast.Name) and test.value.id == "typing"
    return False


@dataclass
class _RawImport:
    module: str | None  # resolved absolute "from" module, or the imported name for ``import x``
    names: tuple[str, ...]  # ``from`` names; empty for ``import x``
    kind: ImportKind
    lineno: int
    function: str | None


class _Collector(ast.NodeVisitor):
    def __init__(self, module: str, is_package: bool) -> None:
        self._package_parts = module.split(".") if is_package else module.split(".")[:-1]
        self._scope: list[str] = []
        self._function_depth = 0
        self._type_checking = 0
        self.imports: list[_RawImport] = []

    def _kind(self) -> ImportKind:
        if self._type_checking:
            return ImportKind.TYPE_CHECKING
        if self._function_depth:
            return ImportKind.LAZY
        return ImportKind.MODULE

    def _function(self) -> str | None:
        return ".".join(self._scope) if self._function_depth else None

    def _visit_scope(self, node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef, is_function: bool) -> None:
        self._scope.append(node.name)
        self._function_depth += int(is_function)
        self.generic_visit(node)
        self._function_depth -= int(is_function)
        self._scope.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_scope(node, is_function=True)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_scope(node, is_function=True)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._visit_scope(node, is_function=False)

    def visit_Lambda(self, node: ast.Lambda) -> None:
        self._function_depth += 1
        self.generic_visit(node)
        self._function_depth -= 1

    def visit_If(self, node: ast.If) -> None:
        if _is_type_checking_test(node.test):
            self._type_checking += 1
            for child in node.body:
                self.visit(child)
            self._type_checking -= 1
            for child in node.orelse:
                self.visit(child)
        else:
            self.generic_visit(node)

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            self.imports.append(_RawImport(alias.name, (), self._kind(), node.lineno, self._function()))

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if node.module == "__future__":
            return
        module: str | None
        if node.level:
            keep = len(self._package_parts) - (node.level - 1)
            if keep < 0:
                raise ValueError(f"relative import beyond top-level package at line {node.lineno}")
            base = self._package_parts[:keep]
            module = ".".join([*base, *([node.module] if node.module else [])])
        else:
            module = node.module
        names = tuple(alias.name for alias in node.names)
        self.imports.append(_RawImport(module, names, self._kind(), node.lineno, self._function()))


def _deepest_existing(name: str, modules: Mapping[str, object]) -> str | None:
    parts = name.split(".")
    for end in range(len(parts), 0, -1):
        candidate = ".".join(parts[:end])
        if candidate in modules:
            return candidate
    return None


def build_graph(root: Path = REPO_ROOT, overrides: Mapping[str, str] | None = None) -> ModuleGraph:
    """Build the import graph. ``overrides`` maps a repo-relative path to
    replacement source (in memory only -- used by the mutation tests; no
    file is ever written). An override for a path that does not exist
    adds a new module."""

    overrides = dict(overrides or {})
    discovered = discover_modules(root)
    for relative in overrides:
        name, is_package = _module_name(Path(relative))
        discovered.setdefault(name, (relative, is_package))

    edges: list[ImportEdge] = []
    for module in sorted(discovered):
        relative, is_package = discovered[module]
        source = overrides.get(relative)
        if source is None:
            source = (root / relative).read_text()
        collector = _Collector(module, is_package)
        collector.visit(_parse(source, relative))
        for raw in collector.imports:
            if raw.module is None:
                continue
            targets: list[str] = []
            if raw.names:
                for imported in raw.names:
                    submodule = f"{raw.module}.{imported}"
                    targets.append(submodule if submodule in discovered else raw.module)
            else:
                targets.append(raw.module)
            for target in targets:
                internal_target = _deepest_existing(target, discovered) if target.split(".")[0] in ROOT_PACKAGES else None
                if internal_target is not None:
                    if internal_target != module:
                        edges.append(ImportEdge(module, internal_target, raw.kind, raw.lineno, raw.function, True))
                else:
                    edges.append(ImportEdge(module, target, raw.kind, raw.lineno, raw.function, False))

    unique = tuple(sorted(set(edges)))
    by_importer: dict[str, list[ImportEdge]] = defaultdict(list)
    for edge in unique:
        by_importer[edge.importer].append(edge)
    return ModuleGraph(
        paths={name: rel for name, (rel, _pkg) in sorted(discovered.items())},
        packages=frozenset(name for name, (_rel, pkg) in discovered.items() if pkg),
        edges=unique,
        by_importer={k: tuple(v) for k, v in sorted(by_importer.items())},
    )


@lru_cache(maxsize=1)
def repository_graph() -> ModuleGraph:
    """The real repository's graph, built once per test session."""

    return build_graph(REPO_ROOT)


# --- closures (R1) -------------------------------------------------------------


@dataclass(frozen=True)
class Reach:
    """How a module or external library was reached from a root."""

    chain: tuple[str, ...]  # human-readable steps, root first
    #: the module this was reached from (``None`` for a root)
    via: str | None = None


def _parents(module: str) -> list[str]:
    parts = module.split(".")
    return [".".join(parts[:end]) for end in range(1, len(parts))]


def import_closure(
    graph: ModuleGraph, roots: Iterable[str], kinds: frozenset[ImportKind] = ALL_KINDS
) -> tuple[dict[str, Reach], dict[str, Reach]]:
    """Breadth-first transitive closure from ``roots`` (sorted, so the
    first-found chain is deterministic). Returns ``(internal modules,
    external top-level libraries)`` with a shortest witness chain each."""

    internal: dict[str, Reach] = {}
    external: dict[str, Reach] = {}
    queue: deque[str] = deque()
    for root in sorted(set(roots)):
        if root in graph.paths and root not in internal:
            internal[root] = Reach((root,))
            queue.append(root)
    while queue:
        module = queue.popleft()
        chain = internal[module].chain
        for parent in _parents(module):
            if parent in graph.paths and parent not in internal:
                internal[parent] = Reach((*chain, f"(package init) {parent}"), via=module)
                queue.append(parent)
        for edge in graph.by_importer.get(module, ()):
            if edge.kind not in kinds:
                continue
            step = f"{edge.where(graph)}"
            if edge.internal:
                if edge.target not in internal:
                    internal[edge.target] = Reach((*chain, step), via=module)
                    queue.append(edge.target)
            else:
                top = edge.target.split(".")[0]
                if top not in external:
                    external[top] = Reach((*chain, step), via=module)
    return internal, external


# --- unit graph and SCCs (R2/R3) ---------------------------------------------


def unit_of(module: str) -> str | None:
    """Dependency unit: ``patchfrog.<top>`` / ``apps.<top>``. The two root
    namespace packages themselves are not units."""

    parts = module.split(".")
    if parts[0] not in ROOT_PACKAGES or len(parts) < 2:
        return None
    return f"{parts[0]}.{parts[1]}"


def unit_edges(graph: ModuleGraph, kinds: frozenset[ImportKind] = ALL_KINDS) -> dict[tuple[str, str], ImportEdge]:
    """Unit-level edges with the first (sorted) import that witnesses each."""

    result: dict[tuple[str, str], ImportEdge] = {}
    for edge in graph.edges:
        if not edge.internal or edge.kind not in kinds:
            continue
        source, target = unit_of(edge.importer), unit_of(edge.target)
        if source is None or target is None or source == target:
            continue
        result.setdefault((source, target), edge)
    return dict(sorted(result.items()))


def strongly_connected_components(edges: Iterable[tuple[str, str]]) -> list[frozenset[str]]:
    """Iterative Tarjan. Returns only components with more than one
    member, sorted by their sorted member list."""

    adjacency: dict[str, list[str]] = defaultdict(list)
    nodes: set[str] = set()
    for source, target in edges:
        adjacency[source].append(target)
        nodes.update((source, target))
    for node in adjacency:
        adjacency[node].sort()

    index: dict[str, int] = {}
    low: dict[str, int] = {}
    on_stack: set[str] = set()
    stack: list[str] = []
    components: list[frozenset[str]] = []
    counter = 0
    for start in sorted(nodes):
        if start in index:
            continue
        work: list[tuple[str, int]] = [(start, 0)]
        index[start] = low[start] = counter
        counter += 1
        stack.append(start)
        on_stack.add(start)
        while work:
            node, child_index = work[-1]
            children = adjacency.get(node, [])
            if child_index < len(children):
                work[-1] = (node, child_index + 1)
                child = children[child_index]
                if child not in index:
                    index[child] = low[child] = counter
                    counter += 1
                    stack.append(child)
                    on_stack.add(child)
                    work.append((child, 0))
                elif child in on_stack:
                    low[node] = min(low[node], index[child])
                continue
            work.pop()
            if work:
                parent = work[-1][0]
                low[parent] = min(low[parent], low[node])
            if low[node] == index[node]:
                members: set[str] = set()
                while True:
                    member = stack.pop()
                    on_stack.discard(member)
                    members.add(member)
                    if member == node:
                        break
                if len(members) > 1:
                    components.append(frozenset(members))
    return sorted(components, key=lambda component: sorted(component))


def path_within(edges: Iterable[tuple[str, str]], start: str, goal: str, allowed: frozenset[str]) -> list[str]:
    adjacency: dict[str, list[str]] = defaultdict(list)
    for source, target in sorted(edges):
        if source in allowed and target in allowed:
            adjacency[source].append(target)
    previous: dict[str, str | None] = {start: None}
    queue: deque[str] = deque([start])
    while queue:
        node = queue.popleft()
        if node == goal:
            break
        for nxt in adjacency[node]:
            if nxt not in previous:
                previous[nxt] = node
                queue.append(nxt)
    if goal not in previous:
        return []
    path: list[str] = []
    cursor: str | None = goal
    while cursor is not None:
        path.append(cursor)
        cursor = previous[cursor]
    return list(reversed(path))


# --- baseline ------------------------------------------------------------------


class SCCBaseline(TypedDict):
    members: list[str]
    #: ``"a -> b"`` -> justification (never empty, never ``TODO...``)
    edges: dict[str, str]


class AppsImportException(TypedDict):
    importer: str
    function: str
    target: str
    reason: str


class Baseline(TypedDict):
    schema_version: int
    measured_at: str
    unit_definition: str
    notes: str
    edge_kinds: list[str]
    historical_sccs: list[SCCBaseline]
    apps_lazy_import_exceptions: list[AppsImportException]


def load_baseline(path: Path = BASELINE_PATH) -> Baseline:
    return cast(Baseline, json.loads(path.read_text()))


def edge_key(source: str, target: str) -> str:
    return f"{source} -> {target}"


def measured_baseline(graph: ModuleGraph, previous: Baseline | None = None) -> Baseline:
    """The baseline document the current tree implies, keeping existing
    justifications and marking anything new ``TODO: ...`` (which the
    tests reject until a reviewer writes a real justification)."""

    old_edges: dict[str, str] = {}
    old_exceptions: dict[tuple[str, str, str], str] = {}
    if previous is not None:
        for old_component in previous["historical_sccs"]:
            old_edges.update(old_component["edges"])
        old_exceptions = {
            (e["importer"], e["function"], e["target"]): e["reason"] for e in previous["apps_lazy_import_exceptions"]
        }

    edges = unit_edges(graph)
    components: list[SCCBaseline] = []
    for component in strongly_connected_components(edges):
        intra = sorted(edge_key(s, t) for (s, t) in edges if s in component and t in component)
        components.append(
            {
                "members": sorted(component),
                "edges": {key: old_edges.get(key, "TODO: justify this new dependency") for key in intra},
            }
        )
    exceptions: list[AppsImportException] = []
    for edge in graph.edges:
        if edge.internal and edge.importer.startswith("patchfrog") and edge.target.startswith("apps.") and edge.kind is ImportKind.LAZY:
            key = (edge.importer, edge.function or "", edge.target)
            exceptions.append(
                {
                    "importer": key[0],
                    "function": key[1],
                    "target": key[2],
                    "reason": old_exceptions.get(key, "TODO: architectural review required"),
                }
            )
    unique_exceptions = {(e["importer"], e["function"], e["target"]): e for e in exceptions}
    return {
        "schema_version": 1,
        "measured_at": date.today().isoformat(),
        "unit_definition": "patchfrog.<top-level package or module> / apps.<top-level package>",
        "notes": previous["notes"] if previous is not None else "",
        "edge_kinds": sorted(kind.value for kind in ImportKind),
        "historical_sccs": components,
        "apps_lazy_import_exceptions": [unique_exceptions[k] for k in sorted(unique_exceptions)],
    }


if __name__ == "__main__":  # pragma: no cover - maintenance helper
    # Print the baseline implied by the current tree (never writes it).
    # Review the diff, replace every "TODO: ..." with a real justification,
    # then save it to tests/architecture/dependency_baseline.json by hand.
    current = measured_baseline(repository_graph(), load_baseline() if BASELINE_PATH.exists() else None)
    json.dump(current, sys.stdout, indent=1, sort_keys=False)
    sys.stdout.write("\n")
