"""Regression tests for the extern cross-file resolution fix.

Root cause (found via the private beta validation sprint's case11
fixture, reproduced and fixed here): a C/C++ file that both
``extern``-declares a function and calls it in the same file (an
extremely common pattern -- declare the prototype, call it, define it
elsewhere) previously resolved the call to its own bare, bodyless
declaration via tier 2's same-file plain-name match, which fires
*before* the repository-wide tiers (4-5) that already existed
specifically to find and collapse onto a real cross-file definition.
The bug meant a real external definition was never even considered when
one happened to also be declared locally -- exactly the situation an
``extern`` declaration exists for.

The fix makes tier 2 defer to the repository-wide tiers when its only
same-file match is a bare, non-static declaration (never itself a real
definition), falling back to that declaration only if no better
definition exists anywhere in the indexed repository. Genuine ambiguity
(two real, unrelated definitions) and internal (``static``) linkage
remain fail-closed exactly as before -- see
:mod:`patchfrog.intelligence.resolution`'s module docstring.
"""

from __future__ import annotations

from patchfrog.domain.code import (
    Language,
    ParsedCall,
    ParsedFile,
    ParsedSymbol,
    SourceSpan,
    SymbolKind,
)
from patchfrog.intelligence.resolution import RepositoryResolver, ResolutionStatus

_SPAN = SourceSpan(start_line=1, end_line=3, start_column=0, end_column=0)


def _c_function(
    name: str, *, visibility: str, parent_qualified_name: str | None = None
) -> ParsedSymbol:
    qualified_name = f"{parent_qualified_name}::{name}" if parent_qualified_name else name
    return ParsedSymbol(
        name=name,
        qualified_name=qualified_name,
        kind=SymbolKind.FUNCTION,
        span=_SPAN,
        signature=None,
        parent_qualified_name=parent_qualified_name,
        visibility=visibility,
        content_hash="hash",
    )


def test_extern_declared_function_resolves_to_its_real_cross_file_definition() -> None:
    """The primary reproduction: conn_manager.c extern-declares and calls
    reconnectWithBackoff(); the real body lives in reconnect_loop.c. The
    call must resolve to the real definition, not conn_manager.c's own
    bodyless declaration."""

    extern_decl = _c_function("reconnectWithBackoff", visibility="declaration")
    call = ParsedCall(callee_name="reconnectWithBackoff", caller_qualified_name=None, line=5, column=4)
    caller_file = ParsedFile(
        path="conn_manager.c", language=Language.C, symbols=(extern_decl,), calls=(call,)
    )
    real_definition = ParsedFile(
        path="reconnect_loop.c",
        language=Language.C,
        symbols=(_c_function("reconnectWithBackoff", visibility="definition"),),
    )

    resolved = RepositoryResolver([caller_file, real_definition]).resolve_calls()

    assert resolved[0].status is ResolutionStatus.RESOLVED
    assert resolved[0].resolved is not None
    assert resolved[0].resolved.file_path == "reconnect_loop.c"


def test_extern_declared_function_falls_back_to_local_declaration_when_no_definition_exists() -> None:
    """No real definition exists anywhere in the indexed repository (a
    genuinely external/library symbol, or an incomplete checkout) -- the
    call still resolves, to the same bare declaration pre-fix behavior
    would have used, rather than becoming spuriously UNRESOLVED."""

    extern_decl = _c_function("redisConnect", visibility="declaration")
    call = ParsedCall(callee_name="redisConnect", caller_qualified_name=None, line=5, column=4)
    caller_file = ParsedFile(
        path="conn_manager.c", language=Language.C, symbols=(extern_decl,), calls=(call,)
    )

    resolved = RepositoryResolver([caller_file]).resolve_calls()

    assert resolved[0].status is ResolutionStatus.RESOLVED
    assert resolved[0].resolved is not None
    assert resolved[0].resolved.file_path == "conn_manager.c"


def test_static_function_in_another_file_never_satisfies_an_unrelated_extern_declaration() -> None:
    """Fail closed: a `static` (internal-linkage) function in a different
    file must never be treated as the real definition behind another
    file's `extern` declaration of the same name -- internal linkage
    means it is, by C/C++ semantics, definitely not the same symbol."""

    extern_decl = _c_function("helper", visibility="declaration")
    call = ParsedCall(callee_name="helper", caller_qualified_name=None, line=5, column=4)
    caller_file = ParsedFile(path="a.c", language=Language.C, symbols=(extern_decl,), calls=(call,))
    unrelated_static = ParsedFile(
        path="b.c", language=Language.C, symbols=(_c_function("helper", visibility="static_definition"),)
    )

    resolved = RepositoryResolver([caller_file, unrelated_static]).resolve_calls()

    # No real (non-static) definition exists anywhere -- falls back to
    # the local declaration, exactly like the "no definition anywhere"
    # case above. It must NOT resolve to b.c's static helper.
    assert resolved[0].status is ResolutionStatus.RESOLVED
    assert resolved[0].resolved is not None
    assert resolved[0].resolved.file_path == "a.c"


def test_two_genuine_definitions_elsewhere_stay_ambiguous_even_with_a_local_extern_declaration() -> None:
    """Two distinct, real (non-static) definitions of the same name in two
    different files, with no #include evidence pointing at either one --
    genuine ambiguity must never be silently resolved to one of them,
    even when the caller's own file also declares the symbol via
    extern."""

    extern_decl = _c_function("helper", visibility="declaration")
    call = ParsedCall(callee_name="helper", caller_qualified_name=None, line=5, column=4)
    caller_file = ParsedFile(path="a.c", language=Language.C, symbols=(extern_decl,), calls=(call,))
    def_b = ParsedFile(path="b.c", language=Language.C, symbols=(_c_function("helper", visibility="definition"),))
    def_c = ParsedFile(path="c.c", language=Language.C, symbols=(_c_function("helper", visibility="definition"),))

    resolved = RepositoryResolver([caller_file, def_b, def_c]).resolve_calls()

    assert resolved[0].status is ResolutionStatus.AMBIGUOUS
    assert resolved[0].resolved is None


def test_static_same_file_declaration_resolves_immediately_without_deferring() -> None:
    """A `static` bare declaration (internal linkage) never defers to
    repository-wide resolution -- its definition, if any, must be in the
    same file by definition, so an unrelated same-named symbol elsewhere
    (even a real external definition) must never win instead."""

    static_decl = _c_function("helper", visibility="static_declaration")
    call = ParsedCall(callee_name="helper", caller_qualified_name=None, line=5, column=4)
    caller_file = ParsedFile(path="a.c", language=Language.C, symbols=(static_decl,), calls=(call,))
    unrelated_definition = ParsedFile(
        path="b.c", language=Language.C, symbols=(_c_function("helper", visibility="definition"),)
    )

    resolved = RepositoryResolver([caller_file, unrelated_definition]).resolve_calls()

    assert resolved[0].status is ResolutionStatus.RESOLVED
    assert resolved[0].resolved is not None
    assert resolved[0].resolved.file_path == "a.c"


def test_namespace_scoped_extern_declaration_falls_back_safely_not_misresolved() -> None:
    """Known limitation, verified rather than silently assumed: tiers 4-5's
    repository-wide pool is restricted to genuinely top-level symbols
    (``parent_qualified_name is None``) -- a namespace-scoped free
    function's extern declaration is therefore excluded from it, same as
    its real cross-file definition under that namespace. The call falls
    back to the local declaration (safe -- never wrong) rather than
    finding the real definition (a real, stated gap; see this fix's PR
    description) or crashing/misresolving to something unrelated."""

    extern_decl = _c_function("computeBackoff", visibility="declaration", parent_qualified_name="policy")
    call = ParsedCall(callee_name="computeBackoff", caller_qualified_name=None, line=5, column=4)
    caller_file = ParsedFile(
        path="conn.cpp", language=Language.CPP, symbols=(extern_decl,), calls=(call,)
    )
    real_definition = ParsedFile(
        path="policy.cpp",
        language=Language.CPP,
        symbols=(_c_function("computeBackoff", visibility="definition", parent_qualified_name="policy"),),
    )

    resolved = RepositoryResolver([caller_file, real_definition]).resolve_calls()

    assert resolved[0].status is ResolutionStatus.RESOLVED
    assert resolved[0].resolved is not None
    assert resolved[0].resolved.file_path == "conn.cpp"
