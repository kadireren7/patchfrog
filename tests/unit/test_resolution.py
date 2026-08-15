from __future__ import annotations

from patchfrog.domain.code import (
    ImportKind,
    Language,
    ParsedCall,
    ParsedFile,
    ParsedImport,
    ParsedSymbol,
    SourceSpan,
    SymbolKind,
)
from patchfrog.intelligence.resolution import RepositoryResolver, ResolutionStatus

_SPAN = SourceSpan(start_line=1, end_line=3, start_column=0, end_column=0)


def _symbol(name: str, kind: SymbolKind = SymbolKind.FUNCTION, parent: str | None = None) -> ParsedSymbol:
    qualified = f"{parent}.{name}" if parent else name
    return ParsedSymbol(
        name=name, qualified_name=qualified, kind=kind, span=_SPAN, signature=None,
        parent_qualified_name=parent, visibility=None, content_hash="hash",
    )


def test_scoped_match_resolves_self_call_within_class() -> None:
    cls = _symbol("Cache", kind=SymbolKind.CLASS)
    get = _symbol("get", kind=SymbolKind.METHOD, parent="Cache")
    helper = _symbol("helper", kind=SymbolKind.METHOD, parent="Cache")
    call = ParsedCall(callee_name="helper", caller_qualified_name="Cache.get", line=2, column=4)
    pf = ParsedFile(path="cache.py", language=Language.PYTHON, symbols=(cls, get, helper), calls=(call,))

    resolved = RepositoryResolver([pf]).resolve_calls()

    assert len(resolved) == 1
    assert resolved[0].status is ResolutionStatus.RESOLVED
    assert resolved[0].resolved is not None
    assert resolved[0].resolved.qualified_name == "Cache.helper"


def test_unique_same_file_match_resolves() -> None:
    fn = _symbol("helper")
    call = ParsedCall(callee_name="helper", caller_qualified_name=None, line=1, column=0)
    pf = ParsedFile(path="mod.py", language=Language.PYTHON, symbols=(fn,), calls=(call,))

    resolved = RepositoryResolver([pf]).resolve_calls()

    assert resolved[0].status is ResolutionStatus.RESOLVED
    assert resolved[0].resolved is not None and resolved[0].resolved.qualified_name == "helper"


def test_ambiguous_same_file_match_is_not_guessed() -> None:
    a = ParsedSymbol(
        name="helper", qualified_name="A.helper", kind=SymbolKind.METHOD, span=_SPAN,
        signature=None, parent_qualified_name="A", visibility=None, content_hash="h",
    )
    b = ParsedSymbol(
        name="helper", qualified_name="B.helper", kind=SymbolKind.METHOD, span=_SPAN,
        signature=None, parent_qualified_name="B", visibility=None, content_hash="h",
    )
    call = ParsedCall(callee_name="helper", caller_qualified_name=None, line=1, column=0)
    pf = ParsedFile(path="mod.py", language=Language.PYTHON, symbols=(a, b), calls=(call,))

    resolved = RepositoryResolver([pf]).resolve_calls()

    assert resolved[0].status is ResolutionStatus.AMBIGUOUS
    assert resolved[0].resolved is None


def test_unresolved_when_no_candidate_symbol_exists() -> None:
    call = ParsedCall(callee_name="missing", caller_qualified_name=None, line=1, column=0)
    pf = ParsedFile(path="mod.py", language=Language.PYTHON, symbols=(), calls=(call,))

    resolved = RepositoryResolver([pf]).resolve_calls()

    assert resolved[0].status is ResolutionStatus.UNRESOLVED
    assert resolved[0].resolved is None


def test_import_based_cross_file_call_resolves() -> None:
    utils_fn = _symbol("normalize_key")
    utils = ParsedFile(path="src/utils.py", language=Language.PYTHON, symbols=(utils_fn,))

    imp = ParsedImport(raw_text="from src.utils import normalize_key", target="src.utils.normalize_key", kind=ImportKind.EXTERNAL, line=1)
    call = ParsedCall(callee_name="normalize_key", caller_qualified_name=None, line=3, column=0)
    cache = ParsedFile(path="src/cache.py", language=Language.PYTHON, imports=(imp,), calls=(call,))

    resolver = RepositoryResolver([utils, cache])
    resolved_calls = resolver.resolve_calls()

    call_result = next(r for r in resolved_calls if r.call.callee_name == "normalize_key")
    assert call_result.status is ResolutionStatus.RESOLVED
    assert call_result.resolved is not None
    assert call_result.resolved.file_path == "src/utils.py"


def test_c_include_resolves_to_local_file() -> None:
    header = ParsedFile(path="src/node.h", language=Language.C)
    imp = ParsedImport(raw_text='#include "node.h"', target="node.h", kind=ImportKind.LOCAL, line=1)
    source = ParsedFile(path="src/list.c", language=Language.C, imports=(imp,))

    resolver = RepositoryResolver([header, source])
    resolved_imports = resolver.resolve_imports()

    result = next(r for r in resolved_imports if r.file_path == "src/list.c")
    assert result.resolved_file_path == "src/node.h"


def test_external_import_is_never_resolved() -> None:
    imp = ParsedImport(raw_text="import os", target="os", kind=ImportKind.EXTERNAL, line=1)
    pf = ParsedFile(path="mod.py", language=Language.PYTHON, imports=(imp,))

    resolved = RepositoryResolver([pf]).resolve_imports()

    assert resolved[0].resolved_file_path is None
