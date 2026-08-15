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
    a = _symbol("helper")
    b = ParsedSymbol(
        name="helper", qualified_name="helper2", kind=SymbolKind.FUNCTION, span=_SPAN,
        signature=None, parent_qualified_name=None, visibility=None, content_hash="h",
    )
    call = ParsedCall(callee_name="helper", caller_qualified_name=None, line=1, column=0)
    pf = ParsedFile(path="mod.py", language=Language.PYTHON, symbols=(a, b), calls=(call,))

    resolved = RepositoryResolver([pf]).resolve_calls()

    assert resolved[0].status is ResolutionStatus.AMBIGUOUS
    assert resolved[0].resolved is None


def test_method_call_is_never_resolved_by_same_file_name_coincidence() -> None:
    """A call like ``other_thing.run()`` must never be resolved just because
    exactly one method named "run" happens to exist in the same file — there
    is zero evidence ``other_thing`` is an instance of that method's class.
    This is a real bug this test reproduces: same-file plain-name matching
    is only sound for free functions, never for methods, since a method
    call's target depends entirely on the receiver's (unknown) type.
    """

    run_method = _symbol("run", kind=SymbolKind.METHOD, parent="A")
    call = ParsedCall(callee_name="run", caller_qualified_name="Unrelated.use_other_object", line=5, column=8)
    pf = ParsedFile(
        path="x.py", language=Language.PYTHON,
        symbols=(_symbol("A", kind=SymbolKind.CLASS), run_method, _symbol("Unrelated", kind=SymbolKind.CLASS)),
        calls=(call,),
    )

    resolved = RepositoryResolver([pf]).resolve_calls()

    assert resolved[0].status is ResolutionStatus.UNRESOLVED
    assert resolved[0].resolved is None


def test_two_same_named_methods_in_one_file_called_bare_are_unresolved_not_ambiguous() -> None:
    # Not AMBIGUOUS either — neither candidate is even eligible without
    # scoped (self/this) evidence, so there's nothing to be ambiguous
    # *between*.
    a = _symbol("helper", kind=SymbolKind.METHOD, parent="A")
    b = ParsedSymbol(
        name="helper", qualified_name="B.helper", kind=SymbolKind.METHOD, span=_SPAN,
        signature=None, parent_qualified_name="B", visibility=None, content_hash="h",
    )
    call = ParsedCall(callee_name="helper", caller_qualified_name=None, line=1, column=0)
    pf = ParsedFile(path="mod.py", language=Language.PYTHON, symbols=(a, b), calls=(call,))

    resolved = RepositoryResolver([pf]).resolve_calls()

    assert resolved[0].status is ResolutionStatus.UNRESOLVED
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


def _c_function(name: str, path: str, *, visibility: str) -> ParsedSymbol:
    return ParsedSymbol(
        name=name, qualified_name=name, kind=SymbolKind.FUNCTION, span=_SPAN, signature=None,
        parent_qualified_name=None, visibility=visibility, content_hash=path,
    )


def test_prototype_and_definition_collapse_to_one_repo_wide_candidate() -> None:
    """A header's function prototype and its .c definition are the *same*
    repository function, not two ambiguous candidates — a call site with no
    #include evidence connecting it to either must still resolve, not
    falsely report ambiguity between a declaration and its own definition.
    """

    header = ParsedFile(path="a.h", language=Language.C, symbols=(_c_function("add", "a.h", visibility="declaration"),))
    source = ParsedFile(path="a.c", language=Language.C, symbols=(_c_function("add", "a.c", visibility="definition"),))
    caller = ParsedFile(
        path="c.c", language=Language.C,
        calls=(ParsedCall(callee_name="add", caller_qualified_name=None, line=1, column=0),),
    )

    resolved = RepositoryResolver([header, source, caller]).resolve_calls()

    assert resolved[0].status is ResolutionStatus.RESOLVED
    assert resolved[0].resolved is not None
    assert resolved[0].resolved.file_path == "a.c"  # resolves to the definition, not the prototype


def test_two_genuine_definitions_with_the_same_name_stay_ambiguous() -> None:
    # Unlike the declaration/definition pair above, two *definitions* really
    # are two different candidate functions -- collapsing them would be a
    # false resolution, not a precision improvement.
    source_a = ParsedFile(path="a.c", language=Language.C, symbols=(_c_function("add", "a.c", visibility="definition"),))
    source_b = ParsedFile(path="b.c", language=Language.C, symbols=(_c_function("add", "b.c", visibility="definition"),))
    caller = ParsedFile(
        path="c.c", language=Language.C,
        calls=(ParsedCall(callee_name="add", caller_qualified_name=None, line=1, column=0),),
    )

    resolved = RepositoryResolver([source_a, source_b, caller]).resolve_calls()

    assert resolved[0].status is ResolutionStatus.AMBIGUOUS
    assert resolved[0].resolved is None


def test_constructor_call_resolves_like_a_plain_function_call() -> None:
    """A bare `ClassName()` constructor call has the exact same
    name-resolution soundness as a bare function call — no receiver to
    be uncertain about — and must not be left unresolved just because
    classes were excluded from the resolvable-target-kinds set."""

    cls = _symbol("Cache", kind=SymbolKind.CLASS)
    call = ParsedCall(callee_name="Cache", caller_qualified_name=None, line=1, column=0)
    pf = ParsedFile(path="x.py", language=Language.PYTHON, symbols=(cls,), calls=(call,))

    resolved = RepositoryResolver([pf]).resolve_calls()

    assert resolved[0].status is ResolutionStatus.RESOLVED
    assert resolved[0].resolved is not None
    assert resolved[0].resolved.qualified_name == "Cache"


def test_two_same_named_classes_in_different_files_stay_ambiguous_as_constructors() -> None:
    cls_a = _symbol("Cache", kind=SymbolKind.CLASS)
    file_a = ParsedFile(path="a.py", language=Language.PYTHON, symbols=(cls_a,))
    cls_b = _symbol("Cache", kind=SymbolKind.CLASS)
    file_b = ParsedFile(path="b.py", language=Language.PYTHON, symbols=(cls_b,))
    caller = ParsedFile(
        path="c.py", language=Language.PYTHON,
        calls=(ParsedCall(callee_name="Cache", caller_qualified_name=None, line=1, column=0),),
    )

    resolved = RepositoryResolver([file_a, file_b, caller]).resolve_calls()

    assert resolved[0].status is ResolutionStatus.AMBIGUOUS
    assert resolved[0].resolved is None
