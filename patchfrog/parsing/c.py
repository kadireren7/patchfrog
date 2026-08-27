"""C parser.

Extracts, at file (translation-unit) scope only — nested/local
declarations are deliberately not treated as symbols:

- function definitions and prototypes/declarations
- struct, union, and enum definitions
- typedefs (including the common ``typedef struct { ... } name;`` form)
- macros (``#define``)
- global variables (top-level declarations without a function declarator)
- ``#include`` directives (local vs. system, by quote style)
- call sites

Function bodies are still walked for nested calls, but local variables
and other in-body declarations are not extracted as symbols.
"""

from __future__ import annotations

import tree_sitter_c as tsc
from tree_sitter import Language as TSLanguage
from tree_sitter import Node, Parser

from patchfrog.domain.code import (
    ImportKind,
    Language,
    ParsedCall,
    ParsedFile,
    ParsedImport,
    ParsedSymbol,
    SymbolKind,
)
from patchfrog.parsing.base import content_hash, has_static_storage_class, node_text, span_of

_C_LANGUAGE = TSLanguage(tsc.language())

_CONDITIONAL_PREPROC_TYPES = ("preproc_ifdef", "preproc_if", "preproc_elif", "preproc_else")


class CParser:
    language = Language.C

    def __init__(self) -> None:
        self._parser = Parser(_C_LANGUAGE)

    def parse_file(self, *, relative_path: str, content: bytes) -> ParsedFile:
        tree = self._parser.parse(content)
        root = tree.root_node

        symbols: list[ParsedSymbol] = []
        imports: list[ParsedImport] = []
        calls: list[ParsedCall] = []
        errors: list[str] = []

        if root.has_error:
            errors.append("syntax error(s) present; structure may be incomplete")

        for child in root.children:
            _extract_top_level(child, symbols, imports)

        _walk_calls(root, None, calls)

        return ParsedFile(
            path=relative_path,
            language=Language.C,
            symbols=tuple(symbols),
            imports=tuple(imports),
            calls=tuple(calls),
            parse_errors=tuple(errors),
        )


def _extract_top_level(
    node: Node, symbols: list[ParsedSymbol], imports: list[ParsedImport]
) -> None:
    if node.type == "preproc_include":
        imp = _extract_include(node)
        if imp is not None:
            imports.append(imp)
        return

    if node.type == "preproc_def":
        name_node = node.child_by_field_name("name")
        name = node_text(name_node)
        if name:
            symbols.append(_symbol(name, SymbolKind.MACRO, node, signature=f"#define {name}"))
        return

    if node.type == "function_definition":
        _extract_function(node, symbols, is_definition=True)
        return

    if node.type == "declaration":
        declarator = node.child_by_field_name("declarator")
        if declarator is not None and _find(declarator, "function_declarator") is not None:
            _extract_function(node, symbols, is_definition=False)
        else:
            _extract_global_variable(node, symbols)
        return

    if node.type == "type_definition":
        _extract_typedef(node, symbols)
        return

    if node.type in ("struct_specifier", "union_specifier", "enum_specifier"):
        _extract_tag(node, symbols)
        return

    if node.type == "linkage_specification":
        # extern "C" { ... } style blocks (tolerated even though this is
        # the C parser — some C headers use it for C++ interop).
        body = node.child_by_field_name("body")
        if body is not None:
            for child in body.children:
                _extract_top_level(child, symbols, imports)
        return

    if node.type in _CONDITIONAL_PREPROC_TYPES:
        # #ifndef/#if/#elif/#else guards wrap their contents as direct
        # children with no separate "body" node — near-universal for
        # header include guards, so skipping these would silently drop
        # everything most real C headers declare.
        for child in node.children:
            _extract_top_level(child, symbols, imports)


def _extract_function(node: Node, symbols: list[ParsedSymbol], *, is_definition: bool) -> None:
    declarator = node.child_by_field_name("declarator")
    func_declarator = _find(declarator, "function_declarator") if declarator else None
    if func_declarator is None:
        return
    name_node = func_declarator.child_by_field_name("declarator")
    name = node_text(name_node)
    if not name:
        return

    body = node.child_by_field_name("body")
    header_end = body.start_byte if body is not None else node.end_byte
    signature = (
        (node.text or b"")[: header_end - node.start_byte]
        .decode("utf-8", errors="replace")
        .rstrip()
        .rstrip(";")
        .strip()
    )

    is_static = has_static_storage_class(node)
    if is_definition:
        visibility = "static_definition" if is_static else "definition"
    else:
        visibility = "static_declaration" if is_static else "declaration"

    symbols.append(
        ParsedSymbol(
            name=name,
            qualified_name=name,
            kind=SymbolKind.FUNCTION,
            span=span_of(node),
            signature=signature,
            parent_qualified_name=None,
            visibility=visibility,
            content_hash=content_hash(node.text or b""),
        )
    )


def _extract_global_variable(node: Node, symbols: list[ParsedSymbol]) -> None:
    declarator = node.child_by_field_name("declarator")
    if declarator is None:
        return
    name_node = _leftmost_identifier(declarator)
    name = node_text(name_node)
    if not name:
        return
    symbols.append(_symbol(name, SymbolKind.VARIABLE, node))


def _extract_typedef(node: Node, symbols: list[ParsedSymbol]) -> None:
    # `typedef struct s_node { ... } t_node;` — the alias is the last
    # type_identifier child; the inner struct/union/enum is also indexed
    # in its own right (by name, if it has a tag).
    inner = next(
        (c for c in node.children if c.type in ("struct_specifier", "union_specifier", "enum_specifier")),
        None,
    )
    if inner is not None:
        _extract_tag(inner, symbols)

    alias_nodes = [c for c in node.children if c.type == "type_identifier"]
    if not alias_nodes:
        return
    alias = alias_nodes[-1]
    name = node_text(alias)
    if name:
        symbols.append(_symbol(name, SymbolKind.TYPE_ALIAS, node, signature=f"typedef ... {name}"))


def _extract_tag(node: Node, symbols: list[ParsedSymbol]) -> None:
    name_node = node.child_by_field_name("name")
    name = node_text(name_node)
    if not name:
        return  # anonymous struct/union/enum with no tag — nothing reliable to record
    kind = {
        "struct_specifier": SymbolKind.STRUCT,
        "union_specifier": SymbolKind.UNION,
        "enum_specifier": SymbolKind.ENUM,
    }[node.type]
    keyword = node.type.split("_")[0]
    symbols.append(_symbol(name, kind, node, signature=f"{keyword} {name}"))


def _symbol(
    name: str, kind: SymbolKind, node: Node, *, signature: str | None = None
) -> ParsedSymbol:
    return ParsedSymbol(
        name=name,
        qualified_name=name,
        kind=kind,
        span=span_of(node),
        signature=signature,
        parent_qualified_name=None,
        visibility=None,
        content_hash=content_hash(node.text or b""),
    )


def _extract_include(node: Node) -> ParsedImport | None:
    for child in node.children:
        if child.type == "string_literal":
            content_node = next((c for c in child.children if c.type == "string_content"), None)
            path = node_text(content_node)
            if path:
                return ParsedImport(
                    raw_text=node_text(node),
                    target=path,
                    kind=ImportKind.LOCAL,
                    line=node.start_point[0] + 1,
                )
        elif child.type == "system_lib_string":
            path = node_text(child).strip("<>")
            if path:
                return ParsedImport(
                    raw_text=node_text(node),
                    target=path,
                    kind=ImportKind.EXTERNAL,
                    line=node.start_point[0] + 1,
                )
    return None


def _walk_calls(node: Node, caller: str | None, calls: list[ParsedCall]) -> None:
    if node.type == "function_definition":
        declarator = node.child_by_field_name("declarator")
        func_declarator = _find(declarator, "function_declarator") if declarator else None
        name = node_text(func_declarator.child_by_field_name("declarator")) if func_declarator else None
        for child in node.children:
            _walk_calls(child, name or caller, calls)
        return

    if node.type == "call_expression":
        function_node = node.child_by_field_name("function")
        callee = _callee_name(function_node)
        if callee:
            calls.append(
                ParsedCall(
                    callee_name=callee,
                    caller_qualified_name=caller,
                    line=node.start_point[0] + 1,
                    column=node.start_point[1],
                )
            )

    for child in node.children:
        _walk_calls(child, caller, calls)


def _callee_name(function_node: Node | None) -> str | None:
    if function_node is None:
        return None
    if function_node.type == "identifier":
        return node_text(function_node)
    if function_node.type == "field_expression":
        return node_text(function_node.child_by_field_name("field"))
    return None


def _find(node: Node, type_name: str) -> Node | None:
    """Depth-first search for the first descendant of ``type_name`` (or self)."""

    if node.type == type_name:
        return node
    for child in node.children:
        found = _find(child, type_name)
        if found is not None:
            return found
    return None


def _leftmost_identifier(node: Node) -> Node | None:
    if node.type == "identifier":
        return node
    for child in node.children:
        found = _leftmost_identifier(child)
        if found is not None:
            return found
    return None
