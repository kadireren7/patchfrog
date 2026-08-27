"""Conservative, repository-wide reference resolution.

Turns raw parsed calls and imports into resolved (or explicitly
unresolved/ambiguous) references, by correlating symbols across every
parsed file in a repository. This is deliberately not a compiler or type
checker — precision is prioritized over recall throughout: an ambiguous
or unverifiable reference is left unresolved rather than guessed.

Resolution tiers for a call, in order (first match wins):

1. **Scoped match** — the caller's own ``parent_qualified_name`` plus the
   callee name forms a real symbol in the same file (e.g. ``self.foo()``
   inside ``ClassName.bar`` resolves against ``ClassName.foo``).
2. **Same-file match** — exactly one symbol (after collapsing any
   same-file declaration/definition pair onto the definition, see below)
   has that plain name. If that one match is itself a real definition
   (or a kind with no declaration/definition distinction, e.g. a
   class/struct constructor call), resolve immediately. If it is a bare,
   non-``static`` declaration — the C/C++ "extern function declared and
   called in the same file, defined elsewhere" pattern — defer to tiers
   3-5 instead of resolving to that bodyless prototype, falling back to
   it only if nothing better is found. Multiple same-file matches:
   ambiguous, not guessed.
3. **Import-based match** — the file has a resolved import/include whose
   imported name matches the callee, and the target file defines a
   matching top-level symbol.
4. **Include-narrowed repo match** — among all repository-wide top-level
   symbols with that name (after the same declaration/definition
   collapse, applied *before* narrowing so a repository-wide-unique real
   definition always wins over a same-named bare declaration in a
   directly-included header), only those in files this file actually
   imports/includes (multiple after narrowing: ambiguous).
5. **Unconstrained repo match** — exactly one repository-wide top-level
   symbol has that name (multiple: ambiguous).

Otherwise the call is unresolved.

Extern cross-file resolution (tiers 2/4-5's declaration/definition
collapse): a C/C++ function's prototype (in a header, or declared
`extern` alongside a call to it in the same file) and its real
definition (in a `.c`/`.cpp` file, possibly a different one) are the
*same* repository function, not two ambiguous candidates or — worse — a
bodyless prototype mistaken for the real thing. Collapsing is symbol-
name-and-kind based and fully deterministic: never embeddings, never an
LLM, and fails closed (stays ambiguous, or falls back to the bare
declaration) whenever more than one real definition could plausibly
match. `static` (C/C++ internal linkage) symbols are excluded from
repository-wide candidate pools entirely (tracked via
`ParsedSymbol.visibility`'s `"static_definition"`/`"static_declaration"`
values) — a `static` function in one file must never satisfy another
file's unresolved call, no matter how the names line up.

**Known limitation, not attempted here**: this resolves *function*
extern declarations/calls only. A bare, non-call identifier reference
(e.g. a macro constant like `RETRY_POLICY_MAX_ATTEMPTS` used only in a
comment or an expression, never called) is not tracked as any kind of
relationship at all — there is no `ParsedReference` concept, only
`ParsedCall`. Extending this to extern global variables and macro
references would need genuine reference extraction added to every
language parser, a materially larger change than this fix's scope.
Also out of scope: namespace-qualified repository-wide matching (tiers
4-5 only ever consider genuinely top-level symbols,
``parent_qualified_name is None`` — a namespace-scoped C++ free
function's extern declaration safely falls back to its own bare
declaration rather than crashing or misresolving, but does not find its
real cross-namespace definition; see
``test_namespace_scoped_extern_declaration_falls_back_safely_not_misresolved``
in ``tests/unit/test_extern_cross_file_resolution.py``).

Import/include resolution: Python absolute/relative imports are resolved
by mapping the repository's file paths to dotted module paths and
matching against those (trying the target as a module, then — if that
fails — as `<module>.<name>` with the name trimmed, since a `from x
import y` target may name either a submodule or a symbol). C/C++ local
includes are resolved relative to the including file's directory, with a
same-path-suffix fallback for repo-wide include roots.
"""

from __future__ import annotations

import posixpath
import re
from collections import defaultdict
from dataclasses import dataclass
from enum import StrEnum
from pathlib import PurePosixPath

from patchfrog.domain.code import (
    ImportKind,
    Language,
    ParsedCall,
    ParsedFile,
    ParsedImport,
    ParsedSymbol,
    SymbolKind,
)

_SEPARATOR = {Language.PYTHON: ".", Language.C: "", Language.CPP: "::"}

_RESOLVABLE_CALL_TARGET_KINDS = {
    SymbolKind.FUNCTION,
    SymbolKind.METHOD,
    # A bare `ClassName(...)` constructor call is a plain identifier call
    # syntactically identical to a bare function call (see
    # `_callee_name` in each language parser) — it carries the exact
    # same name-resolution soundness, with no receiver-type ambiguity to
    # worry about (unlike `obj.method()`, see the METHOD exclusion from
    # same-file matching below). Excluding classes/structs here was
    # found to leave ~15% of a real repository's unresolved calls as
    # unnecessarily unresolved constructor calls.
    SymbolKind.CLASS,
    SymbolKind.STRUCT,
}
_RELATIVE_IMPORT_RE = re.compile(r"^(\.+)(.*)$")

#: A symbol with one of these ``visibility`` values has internal (C/C++
#: ``static``) linkage -- it must never be treated as a candidate
#: definition for a call/reference from a *different* file. Same-file
#: resolution is unaffected (see ``_symbols_by_name_in_file`` below,
#: which is never filtered by linkage).
_INTERNAL_LINKAGE_VISIBILITY = frozenset({"static_definition", "static_declaration"})
#: A symbol with this ``visibility`` is a bare prototype/forward
#: declaration with no body -- never itself a resolution target when a
#: real definition exists elsewhere (see ``_collapse_declarations_onto_definition``
#: and the extern cross-file resolution in ``_resolve_one_call``).
_BARE_DECLARATION_VISIBILITY = frozenset({"declaration", "static_declaration"})
_DEFINITION_VISIBILITY = frozenset({"definition", "static_definition"})


class ResolutionStatus(StrEnum):
    RESOLVED = "resolved"
    UNRESOLVED = "unresolved"
    AMBIGUOUS = "ambiguous"


@dataclass(frozen=True, slots=True)
class SymbolRef:
    file_path: str
    qualified_name: str


@dataclass(frozen=True, slots=True)
class ResolvedCall:
    file_path: str
    call: ParsedCall
    status: ResolutionStatus
    resolved: SymbolRef | None = None


@dataclass(frozen=True, slots=True)
class ResolvedImport:
    file_path: str
    import_: ParsedImport
    resolved_file_path: str | None = None


class RepositoryResolver:
    """Resolves calls and imports across a fully-parsed repository.

    Construct once per indexing run with every parsed file, then call
    :meth:`resolve_imports` before :meth:`resolve_calls` (call resolution
    tier 3 depends on already-resolved imports).
    """

    def __init__(self, parsed_files: list[ParsedFile]) -> None:
        self._parsed_files = parsed_files
        self._symbol_by_qualified: dict[tuple[str, str], ParsedSymbol] = {}
        self._symbols_by_name: dict[str, list[SymbolRef]] = defaultdict(list)
        self._symbols_by_name_in_file: dict[tuple[str, str], list[SymbolRef]] = defaultdict(list)

        for pf in parsed_files:
            for sym in pf.symbols:
                if sym.kind not in _RESOLVABLE_CALL_TARGET_KINDS:
                    continue
                ref = SymbolRef(pf.path, sym.qualified_name)
                self._symbol_by_qualified[(pf.path, sym.qualified_name)] = sym
                if sym.visibility not in _INTERNAL_LINKAGE_VISIBILITY:
                    # A `static` (C/C++ internal-linkage) symbol must never
                    # satisfy a *different* file's unresolved call/reference
                    # — same name, same kind, but definitively not the same
                    # symbol. Same-file resolution (below) is unaffected;
                    # only the repository-wide pool excludes it.
                    self._symbols_by_name[sym.name].append(ref)
                if sym.kind is not SymbolKind.METHOD:
                    # Tier 2 (below) matches by plain name within a file
                    # with no evidence about a call's receiver — safe for a
                    # bare function call, but a method call's target
                    # depends entirely on the receiver's type, which
                    # same-file-name matching cannot establish. A same-file
                    # method call is only ever resolved via tier 1's scoped
                    # `self`/`this` match, never by coincidence of name.
                    self._symbols_by_name_in_file[(pf.path, sym.name)].append(ref)

        self._module_index = _build_python_module_index(parsed_files)
        self._all_paths = {pf.path for pf in parsed_files}
        self._resolved_imports_by_file: dict[str, list[ResolvedImport]] = {}

    def resolve_imports(self) -> list[ResolvedImport]:
        results: list[ResolvedImport] = []
        for pf in self._parsed_files:
            resolved_for_file: list[ResolvedImport] = []
            for imp in pf.imports:
                target_path = self._resolve_import(pf, imp)
                resolved_for_file.append(ResolvedImport(pf.path, imp, target_path))
            self._resolved_imports_by_file[pf.path] = resolved_for_file
            results.extend(resolved_for_file)
        return results

    def resolve_calls(self) -> list[ResolvedCall]:
        if not self._resolved_imports_by_file:
            self.resolve_imports()
        return [
            self._resolve_one_call(pf, call) for pf in self._parsed_files for call in pf.calls
        ]

    def _resolve_import(self, pf: ParsedFile, imp: ParsedImport) -> str | None:
        if pf.language is Language.PYTHON:
            return _resolve_python_import(pf.path, imp, self._module_index)
        if imp.kind is ImportKind.LOCAL:
            return _resolve_c_include(pf.path, imp.target, self._all_paths)
        return None

    def _resolve_one_call(self, pf: ParsedFile, call: ParsedCall) -> ResolvedCall:
        sep = _SEPARATOR[pf.language]

        # Tier 1: scoped match via the caller's own parent.
        if call.caller_qualified_name is not None and sep:
            caller_symbol = self._symbol_by_qualified.get((pf.path, call.caller_qualified_name))
            if caller_symbol is not None and caller_symbol.parent_qualified_name is not None:
                candidate_qn = f"{caller_symbol.parent_qualified_name}{sep}{call.callee_name}"
                if (pf.path, candidate_qn) in self._symbol_by_qualified:
                    return ResolvedCall(
                        pf.path, call, ResolutionStatus.RESOLVED, SymbolRef(pf.path, candidate_qn)
                    )

        # Tier 2: unique same-file match by plain name. Collapsed the same
        # way as tiers 4-5 (a same-file forward declaration plus its own
        # same-file definition are one symbol, not two ambiguous ones).
        same_file = self._collapse_declarations_onto_definition(
            self._symbols_by_name_in_file.get((pf.path, call.callee_name), [])
        )
        same_file_declaration_fallback: SymbolRef | None = None
        if len(same_file) == 1:
            match = same_file[0]
            match_symbol = self._symbol_by_qualified[(match.file_path, match.qualified_name)]
            if match_symbol.visibility != "declaration":
                # A real definition (static or not), a static bare
                # declaration, or a kind with no declaration/definition
                # distinction (e.g. a class/struct constructor call) --
                # resolve immediately, exactly as before. A *static* bare
                # declaration is also resolved immediately: `static`
                # linkage means its definition (if any) must be in this
                # same file too, so there is nothing to gain from
                # deferring to repository-wide resolution.
                return ResolvedCall(pf.path, call, ResolutionStatus.RESOLVED, match)
            # A bare, non-static ("extern" or unspecified-linkage)
            # same-file declaration -- e.g. `extern int foo(void);`
            # followed by a call to `foo()` in the same file. This is
            # never itself a real callable target (no body), and C/C++
            # give it external linkage by default, so a real definition
            # may exist in another file. Defer to repository-wide
            # resolution (tiers 3-5) below, which can find and collapse
            # onto that real definition; fall back to this bare
            # declaration only if nothing better exists anywhere.
            same_file_declaration_fallback = match
        elif len(same_file) > 1:
            return ResolvedCall(pf.path, call, ResolutionStatus.AMBIGUOUS)

        # Tier 3: import-based match — an imported name resolving to a
        # file that defines a matching top-level symbol.
        for resolved_import in self._resolved_imports_by_file.get(pf.path, []):
            if resolved_import.resolved_file_path is None:
                continue
            imported_name = resolved_import.import_.target.rsplit(".", 1)[-1].lstrip(".")
            if imported_name != call.callee_name:
                continue
            candidate = self._symbol_by_qualified.get(
                (resolved_import.resolved_file_path, call.callee_name)
            )
            if candidate is not None and candidate.parent_qualified_name is None:
                return ResolvedCall(
                    pf.path,
                    call,
                    ResolutionStatus.RESOLVED,
                    SymbolRef(resolved_import.resolved_file_path, call.callee_name),
                )

        # Tiers 4-5: repository-wide match by plain name, restricted to
        # top-level symbols (nested/member symbols require real scoping,
        # already attempted in tier 1). Internal-linkage (`static`)
        # symbols are already excluded from ``_symbols_by_name`` entirely
        # (see the constructor), so every candidate here is genuinely a
        # cross-file resolution possibility.
        repo_candidates = [
            ref
            for ref in self._symbols_by_name.get(call.callee_name, [])
            if self._symbol_by_qualified[(ref.file_path, ref.qualified_name)].parent_qualified_name
            is None
        ]
        if not repo_candidates:
            if same_file_declaration_fallback is not None:
                # No real definition exists anywhere in the indexed
                # repository (e.g. a genuinely external/library symbol,
                # or an incomplete/partial checkout) -- the bare same-file
                # declaration deferred above is the best available
                # answer, exactly what pre-fix behavior would have
                # returned for this call.
                return ResolvedCall(pf.path, call, ResolutionStatus.RESOLVED, same_file_declaration_fallback)
            return ResolvedCall(pf.path, call, ResolutionStatus.UNRESOLVED)

        # Collapse declaration/definition pairs *before* narrowing by
        # `#include`/import evidence, not after: a repository-wide-unique
        # real definition is always a better answer than a same-named
        # bare declaration in a directly-included header, whether or not
        # the caller happens to include that specific header (the
        # "extern function declared here, defined over there" pattern —
        # see this module's docstring and the regression tests in
        # tests/unit/test_extern_cross_file_resolution.py).
        collapsed = self._collapse_declarations_onto_definition(repo_candidates)
        if len(collapsed) == 1:
            return ResolvedCall(pf.path, call, ResolutionStatus.RESOLVED, collapsed[0])

        included_paths = {
            ri.resolved_file_path
            for ri in self._resolved_imports_by_file.get(pf.path, [])
            if ri.resolved_file_path is not None
        }
        narrowed = [c for c in collapsed if c.file_path in included_paths]
        pool = narrowed or collapsed
        if len(pool) == 1:
            return ResolvedCall(pf.path, call, ResolutionStatus.RESOLVED, pool[0])
        return ResolvedCall(pf.path, call, ResolutionStatus.AMBIGUOUS)

    def _collapse_declarations_onto_definition(self, candidates: list[SymbolRef]) -> list[SymbolRef]:
        """A C/C++ function's prototype (in a header) and its definition (in
        a ``.c``/``.cpp`` file) are two distinct top-level symbols, but the
        *same repository function* — they must not read as two ambiguous
        candidates. Collapse the pool down to the one definition only when
        it's unambiguous which definition the declarations belong to
        (exactly one candidate is a definition -- ``visibility`` in
        :data:`_DEFINITION_VISIBILITY` -- and every other candidate is a
        bare declaration -- ``visibility`` in
        :data:`_BARE_DECLARATION_VISIBILITY`). If two or more candidates
        are themselves definitions, that is genuine ambiguity — e.g. two
        same-named ``static`` functions in different files with no import
        evidence to disambiguate — and is left untouched."""

        if len(candidates) < 2:
            return candidates
        definitions = [
            c
            for c in candidates
            if self._symbol_by_qualified[(c.file_path, c.qualified_name)].visibility in _DEFINITION_VISIBILITY
        ]
        if len(definitions) != 1:
            return candidates
        non_definitions = [c for c in candidates if c not in definitions]
        if all(
            self._symbol_by_qualified[(c.file_path, c.qualified_name)].visibility
            in _BARE_DECLARATION_VISIBILITY
            for c in non_definitions
        ):
            return definitions
        return candidates


def _build_python_module_index(parsed_files: list[ParsedFile]) -> dict[str, str]:
    index: dict[str, str] = {}
    for pf in parsed_files:
        if pf.language is not Language.PYTHON:
            continue
        index[_file_to_module_path(pf.path)] = pf.path
    return index


def _file_to_module_path(path: str) -> str:
    p = PurePosixPath(path)
    parts = p.parts[:-1] if p.name == "__init__.py" else (*p.parts[:-1], p.stem)
    return ".".join(parts)


def _resolve_python_import(
    file_path: str, imp: ParsedImport, module_index: dict[str, str]
) -> str | None:
    match = _RELATIVE_IMPORT_RE.match(imp.target)
    if match and match.group(1):
        dots, rest = match.groups()
        is_init = PurePosixPath(file_path).name == "__init__.py"
        base_parts = _file_to_module_path(file_path).split(".")
        containing_package = base_parts if is_init else base_parts[:-1]
        strip = len(dots) - 1
        if strip > len(containing_package):
            return None
        package_parts = containing_package[: len(containing_package) - strip] if strip else containing_package
        target_parts = [*package_parts, *rest.split(".")] if rest else package_parts
    else:
        target_parts = imp.target.split(".")

    target_module = ".".join(p for p in target_parts if p)
    if target_module in module_index:
        return module_index[target_module]

    if "." in target_module:
        trimmed = target_module.rsplit(".", 1)[0]
        if trimmed in module_index:
            return module_index[trimmed]

    return None


def _resolve_c_include(file_path: str, target: str, all_paths: set[str]) -> str | None:
    importer_dir = PurePosixPath(file_path).parent
    candidate = posixpath.normpath(str(importer_dir / target))
    if candidate in all_paths:
        return candidate

    matches = [p for p in all_paths if p == target or p.endswith("/" + target)]
    if len(matches) == 1:
        return matches[0]
    return None
