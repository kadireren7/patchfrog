"""Blast radius (M6.6): how far a change travels from its direct consumers.

Built only from evidence:

- **DIRECT** -- symbols (or files, for module-level usage) containing an
  affected usage site (from :mod:`patchfrog.upstream.consumers`);
- **TRANSITIVE** -- callers of a direct symbol through *resolved*
  ``SYMBOL_CALLS_SYMBOL`` edges of the existing repository graph, up to
  ``max_depth`` (confidence decays per hop);
- **POTENTIAL** -- sites whose only evidence is version-level or a
  credential variable name. They are never expanded: a caller of a
  potential consumer has no evidence at all.

Callees of direct symbols are listed separately and never counted as
impact (they may build the arguments of the changed call, but nothing
proves it). Related tests come from the graph's test relationships
(filename/import evidence) plus tests that are themselves consumers or
callers. Files without a call graph (JS/TS today) are reported as
coverage gaps, never assumed to have no callers.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any

from patchfrog.dependencies.domain import DetectionConfidence
from patchfrog.indexing.inventory import is_test_path
from patchfrog.upstream.code_graph import EMPTY_GRAPH, LocalCodeGraph, SymbolRef
from patchfrog.upstream.consumers import AffectedConsumer, ConsumerImpact, ImpactKind

HIGH = DetectionConfidence.HIGH
MEDIUM = DetectionConfidence.MEDIUM
LOW = DetectionConfidence.LOW
_RANK = {LOW: 0, MEDIUM: 1, HIGH: 2}

DEFAULT_MAX_DEPTH = 2
DEFAULT_MAX_NODES = 200


def node_key(file_path: str, symbol: str | None) -> str:
    return f"{file_path}::{symbol}" if symbol else file_path


@dataclass(frozen=True, slots=True)
class BlastNode:
    file_path: str
    symbol: str | None
    impact: ImpactKind
    confidence: DetectionConfidence
    depth: int
    is_test: bool
    reasons: tuple[str, ...]

    @property
    def key(self) -> str:
        return node_key(self.file_path, self.symbol)


@dataclass(frozen=True, slots=True)
class BlastEdge:
    source: str
    target: str
    #: ``uses_changed_contract`` | ``called_by``
    relation: str
    impact: ImpactKind
    confidence: DetectionConfidence
    evidence: str


@dataclass(frozen=True, slots=True)
class RelatedTest:
    file_path: str
    covers: str
    reason: str
    confidence: DetectionConfidence


@dataclass(frozen=True, slots=True)
class BlastRadius:
    repository: str
    dependency_key: str
    nodes: tuple[BlastNode, ...]
    edges: tuple[BlastEdge, ...]
    direct_site_keys: tuple[str, ...]
    related_tests: tuple[RelatedTest, ...]
    modules: tuple[str, ...]
    direct_callees: tuple[str, ...]
    files_without_call_graph: tuple[str, ...]
    graph_languages: tuple[str, ...]
    truncated: bool = False

    def _of(self, impact: ImpactKind, *, tests: bool = False) -> tuple[BlastNode, ...]:
        return tuple(n for n in self.nodes if n.impact is impact and n.is_test == tests)

    @property
    def direct(self) -> tuple[BlastNode, ...]:
        return self._of(ImpactKind.DIRECT)

    @property
    def transitive(self) -> tuple[BlastNode, ...]:
        return self._of(ImpactKind.TRANSITIVE)

    @property
    def potential(self) -> tuple[BlastNode, ...]:
        return self._of(ImpactKind.POTENTIAL)

    def summary(self) -> dict[str, int]:
        return {
            "direct_sites": len(self.direct_site_keys),
            "direct": len(self.direct),
            "transitive": len(self.transitive),
            "potential": len(self.potential),
            "related_tests": len({t.file_path for t in self.related_tests}),
            "modules": len(self.modules),
            "files": len({n.file_path for n in self.nodes if not n.is_test}),
        }


def _group(consumers: Iterable[AffectedConsumer]) -> dict[tuple[str, str | None], list[AffectedConsumer]]:
    grouped: dict[tuple[str, str | None], list[AffectedConsumer]] = {}
    for consumer in consumers:
        grouped.setdefault((consumer.file_path, consumer.symbol), []).append(consumer)
    return grouped


def _decay(confidence: DetectionConfidence, depth: int) -> DetectionConfidence:
    if depth <= 1:
        return MEDIUM if confidence is not LOW else LOW
    return LOW


def compute_blast_radius(
    impact: ConsumerImpact,
    graph: LocalCodeGraph = EMPTY_GRAPH,
    *,
    max_depth: int = DEFAULT_MAX_DEPTH,
    max_nodes: int = DEFAULT_MAX_NODES,
) -> BlastRadius:
    origin = f"external:{impact.dependency_key}"
    nodes: dict[str, BlastNode] = {}
    edges: list[BlastEdge] = []

    for (file_path, symbol), consumers in sorted(_group(impact.direct).items(), key=lambda kv: (kv[0][0], kv[0][1] or "")):
        confidence = max((c.confidence for c in consumers), key=lambda c: _RANK[c])
        node = BlastNode(file_path, symbol, ImpactKind.DIRECT, confidence, 0, is_test_path(file_path),
                         tuple(sorted({r for c in consumers for r in c.reasons})))
        nodes[node.key] = node
        edges.append(BlastEdge(origin, node.key, "uses_changed_contract", ImpactKind.DIRECT, confidence,
                               ", ".join(sorted({t.value for c in consumers for t in c.match_types}))))

    truncated = False
    frontier: list[tuple[SymbolRef, DetectionConfidence]] = [
        ((n.file_path, n.symbol), n.confidence) for n in nodes.values() if n.symbol is not None
    ]
    for depth in range(1, max_depth + 1):
        next_frontier: list[tuple[SymbolRef, DetectionConfidence]] = []
        for (file_path, symbol), confidence in frontier:
            for caller_file, caller_symbol in sorted(graph.callers_of((file_path, symbol))):
                key = node_key(caller_file, caller_symbol)
                edge_confidence = _decay(confidence, depth)
                edges.append(BlastEdge(node_key(file_path, symbol), key, "called_by", ImpactKind.TRANSITIVE,
                                       edge_confidence, "resolved call edge"))
                if key in nodes:
                    continue
                if len(nodes) >= max_nodes:
                    truncated = True
                    continue
                nodes[key] = BlastNode(caller_file, caller_symbol, ImpactKind.TRANSITIVE, edge_confidence, depth,
                                       is_test_path(caller_file), (f"calls {node_key(file_path, symbol)}",))
                next_frontier.append(((caller_file, caller_symbol), edge_confidence))
        frontier = next_frontier

    for (file_path, symbol), consumers in sorted(_group(impact.potential).items(), key=lambda kv: (kv[0][0], kv[0][1] or "")):
        key = node_key(file_path, symbol)
        if key in nodes:
            continue
        node = BlastNode(file_path, symbol, ImpactKind.POTENTIAL, LOW, 0, is_test_path(file_path),
                         tuple(sorted({r for c in consumers for r in c.reasons})))
        nodes[key] = node
        edges.append(BlastEdge(origin, key, "uses_changed_contract", ImpactKind.POTENTIAL, LOW,
                               ", ".join(sorted({t.value for c in consumers for t in c.match_types}))))

    affected_symbols = {(n.file_path, n.symbol) for n in nodes.values() if n.symbol}
    callees = sorted(
        {
            node_key(*callee)
            for n in nodes.values() if n.impact is ImpactKind.DIRECT and n.symbol
            for callee in graph.callees_of((n.file_path, n.symbol))
            if callee not in affected_symbols
        }
    )

    related: dict[tuple[str, str], RelatedTest] = {}
    impacted = [n for n in nodes.values() if n.impact is not ImpactKind.POTENTIAL]
    for node in impacted:
        if node.is_test:
            reason = (
                "test consumes the changed contract directly" if node.impact is ImpactKind.DIRECT
                else "test calls an affected symbol"
            )
            related[(node.file_path, node.key)] = RelatedTest(
                node.file_path, node.key, reason, HIGH if node.impact is ImpactKind.DIRECT else MEDIUM)
    for file_path in sorted({n.file_path for n in impacted if not n.is_test}):
        for test_file, reason in sorted(graph.tests_for_file.get(file_path, set())):
            if (test_file, file_path) in related or any(t == test_file for t, _ in related):
                continue
            related[(test_file, file_path)] = RelatedTest(
                test_file, file_path, f"tests {file_path} ({reason})",
                MEDIUM if "import" in reason else LOW,
            )

    production = [n for n in impacted if not n.is_test]
    modules = sorted({str(PurePosixPath(n.file_path).parent) for n in production})
    without_graph = sorted(
        {n.file_path for n in nodes.values() if n.impact is ImpactKind.DIRECT and not graph.has_call_graph(n.file_path)}
    )
    return BlastRadius(
        repository=impact.repository,
        dependency_key=impact.dependency_key,
        nodes=tuple(sorted(nodes.values(), key=lambda n: (n.impact.value, n.depth, n.key))),
        edges=tuple(sorted(set(edges), key=lambda e: (e.relation, e.source, e.target))),
        direct_site_keys=tuple(sorted(c.site_key for c in impact.direct)),
        related_tests=tuple(sorted(related.values(), key=lambda t: (t.file_path, t.covers))),
        modules=tuple(modules),
        direct_callees=tuple(callees),
        files_without_call_graph=tuple(without_graph),
        graph_languages=tuple(sorted(graph.languages)),
        truncated=truncated,
    )


def blast_radius_to_dict(radius: BlastRadius) -> dict[str, Any]:
    return {
        "repository": radius.repository,
        "dependency": radius.dependency_key,
        "summary": radius.summary(),
        "nodes": [
            {"node": n.key, "impact": n.impact.value, "confidence": n.confidence.value, "depth": n.depth,
             "is_test": n.is_test, "reasons": list(n.reasons)}
            for n in radius.nodes
        ],
        "edges": [
            {"source": e.source, "target": e.target, "relation": e.relation, "impact": e.impact.value,
             "confidence": e.confidence.value, "evidence": e.evidence}
            for e in radius.edges
        ],
        "related_tests": [
            {"file": t.file_path, "covers": t.covers, "reason": t.reason, "confidence": t.confidence.value}
            for t in radius.related_tests
        ],
        "modules": list(radius.modules),
        "direct_callees_not_counted": list(radius.direct_callees),
        "coverage": {
            "graph_languages": list(radius.graph_languages),
            "files_without_call_graph": list(radius.files_without_call_graph),
            "truncated": radius.truncated,
        },
    }


__all__ = [
    "DEFAULT_MAX_DEPTH",
    "DEFAULT_MAX_NODES",
    "BlastEdge",
    "BlastNode",
    "BlastRadius",
    "RelatedTest",
    "blast_radius_to_dict",
    "compute_blast_radius",
    "node_key",
]
