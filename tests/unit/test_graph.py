from __future__ import annotations

from patchfrog.domain.code import Language, ParsedFile, ParsedSymbol, SourceSpan, SymbolKind
from patchfrog.intelligence.graph import EdgeKind, GraphNode, NodeKind, build_graph
from patchfrog.intelligence.resolution import (
    ResolutionStatus,
    ResolvedCall,
    ResolvedImport,
    SymbolRef,
)
from patchfrog.intelligence.tests import TestRelationship as SourceTestRelationship

_SPAN = SourceSpan(start_line=1, end_line=3, start_column=0, end_column=0)


def _symbol(name: str, parent: str | None = None) -> ParsedSymbol:
    qualified = f"{parent}.{name}" if parent else name
    return ParsedSymbol(
        name=name, qualified_name=qualified, kind=SymbolKind.METHOD, span=_SPAN,
        signature=None, parent_qualified_name=parent, visibility=None, content_hash="h",
    )


def test_symbol_containment_edge_created() -> None:
    parent = _symbol("Cache")
    child = _symbol("get", parent="Cache")
    pf = ParsedFile(path="cache.py", language=Language.PYTHON, symbols=(parent, child))

    edges = build_graph(parsed_files=[pf], resolved_imports=[], resolved_calls=[], test_relationships=[])

    containment = [e for e in edges if e.kind is EdgeKind.SYMBOL_CONTAINS_SYMBOL]
    assert len(containment) == 1
    assert containment[0].source == GraphNode(NodeKind.SYMBOL, "cache.py", "Cache")
    assert containment[0].target == GraphNode(NodeKind.SYMBOL, "cache.py", "Cache.get")


def test_resolved_include_creates_file_edge() -> None:
    from patchfrog.domain.code import ImportKind, ParsedImport

    imp = ParsedImport(raw_text='#include "node.h"', target="node.h", kind=ImportKind.LOCAL, line=1)
    resolved_import = ResolvedImport(file_path="list.c", import_=imp, resolved_file_path="node.h")

    edges = build_graph(parsed_files=[], resolved_imports=[resolved_import], resolved_calls=[], test_relationships=[])

    assert len(edges) == 1
    assert edges[0].kind is EdgeKind.FILE_INCLUDES_FILE
    assert edges[0].source == GraphNode(NodeKind.FILE, "list.c")
    assert edges[0].target == GraphNode(NodeKind.FILE, "node.h")


def test_unresolved_import_creates_no_edge() -> None:
    from patchfrog.domain.code import ImportKind, ParsedImport

    imp = ParsedImport(raw_text="import os", target="os", kind=ImportKind.EXTERNAL, line=1)
    resolved_import = ResolvedImport(file_path="mod.py", import_=imp, resolved_file_path=None)

    edges = build_graph(parsed_files=[], resolved_imports=[resolved_import], resolved_calls=[], test_relationships=[])

    assert edges == []


def test_resolved_call_creates_symbol_calls_symbol_edge() -> None:
    from patchfrog.domain.code import ParsedCall

    call = ParsedCall(callee_name="helper", caller_qualified_name="main", line=2, column=0)
    resolved_call = ResolvedCall(
        file_path="mod.py", call=call, status=ResolutionStatus.RESOLVED,
        resolved=SymbolRef(file_path="mod.py", qualified_name="helper"),
    )

    edges = build_graph(parsed_files=[], resolved_imports=[], resolved_calls=[resolved_call], test_relationships=[])

    assert len(edges) == 1
    assert edges[0].kind is EdgeKind.SYMBOL_CALLS_SYMBOL
    assert edges[0].source == GraphNode(NodeKind.SYMBOL, "mod.py", "main")
    assert edges[0].target == GraphNode(NodeKind.SYMBOL, "mod.py", "helper")


def test_unresolved_call_creates_no_edge() -> None:
    from patchfrog.domain.code import ParsedCall

    call = ParsedCall(callee_name="missing", caller_qualified_name="main", line=2, column=0)
    resolved_call = ResolvedCall(file_path="mod.py", call=call, status=ResolutionStatus.UNRESOLVED)

    edges = build_graph(parsed_files=[], resolved_imports=[], resolved_calls=[resolved_call], test_relationships=[])

    assert edges == []


def test_test_relationship_creates_file_tests_file_edge() -> None:
    relationship = SourceTestRelationship(
        test_file_path="tests/test_cache.py", source_file_path="src/cache.py", reason="filename pattern"
    )

    edges = build_graph(parsed_files=[], resolved_imports=[], resolved_calls=[], test_relationships=[relationship])

    assert len(edges) == 1
    assert edges[0].kind is EdgeKind.FILE_TESTS_FILE
    assert edges[0].source == GraphNode(NodeKind.FILE, "tests/test_cache.py")
    assert edges[0].target == GraphNode(NodeKind.FILE, "src/cache.py")
    assert edges[0].reason == "filename pattern"
