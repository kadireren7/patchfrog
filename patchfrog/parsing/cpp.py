"""C++ parser.

Extracts namespaces, classes/structs (and their in-class method
declarations), out-of-class method definitions (qualified by
``Namespace::Class::method``), free functions, constructors/destructors
(as methods — the domain model has no separate kind for them), enums,
and templates (the wrapped function/class is extracted normally; the
``template<...>`` header is folded into its signature). Extracts
``#include`` directives and call sites, same as the C parser.

Local variables and other function-body declarations are not extracted
as symbols — only namespace/class/function-level constructs are. Call
sites are attributed using the *same* namespace/class-qualified name as
symbols (`_Scope.caller`), so a caller_qualified_name can always be
matched against a symbol's qualified_name by the resolution layer.
"""

from __future__ import annotations

from dataclasses import dataclass

import tree_sitter_cpp as tscpp
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
from patchfrog.parsing.base import content_hash, node_text, span_of

_CPP_LANGUAGE = TSLanguage(tscpp.language())

_CONTAINER_KINDS = {SymbolKind.CLASS, SymbolKind.STRUCT}


@dataclass(frozen=True, slots=True)
class _Scope:
    qualified_name: str | None
    kind: SymbolKind | None
    caller: str | None


def _qualify(parent: str | None, name: str) -> str:
    return f"{parent}::{name}" if parent else name


class CppParser:
    language = Language.CPP

    def __init__(self) -> None:
        self._parser = Parser(_CPP_LANGUAGE)

    def parse_file(self, *, relative_path: str, content: bytes) -> ParsedFile:
        tree = self._parser.parse(content)
        root = tree.root_node

        symbols: list[ParsedSymbol] = []
        imports: list[ParsedImport] = []
        calls: list[ParsedCall] = []
        errors: list[str] = []

        if root.has_error:
            errors.append("syntax error(s) present; structure may be incomplete")

        _walk(root, _Scope(None, None, None), symbols, imports, calls)

        return ParsedFile(
            path=relative_path,
            language=Language.CPP,
            symbols=tuple(symbols),
            imports=tuple(imports),
            calls=tuple(calls),
            parse_errors=tuple(errors),
        )


def _walk(
    node: Node,
    scope: _Scope,
    symbols: list[ParsedSymbol],
    imports: list[ParsedImport],
    calls: list[ParsedCall],
) -> None:
    if node.type == "preproc_include":
        imp = _extract_include(node)
        if imp is not None:
            imports.append(imp)
        return

    if node.type == "call_expression":
        callee = _callee_name(node.child_by_field_name("function"))
        if callee:
            calls.append(
                ParsedCall(
                    callee_name=callee,
                    caller_qualified_name=scope.caller,
                    line=node.start_point[0] + 1,
                    column=node.start_point[1],
                )
            )
        for child in node.children:
            _walk(child, scope, symbols, imports, calls)
        return

    if node.type == "namespace_definition":
        name_node = node.child_by_field_name("name")
        name = node_text(name_node)
        qualified_name = _qualify(scope.qualified_name, name) if name else scope.qualified_name
        body = node.child_by_field_name("body")
        if body is not None:
            child_scope = _Scope(qualified_name, SymbolKind.MODULE, scope.caller)
            for child in body.children:
                _walk(child, child_scope, symbols, imports, calls)
        return

    if node.type == "template_declaration":
        # Fold the `template<...>` header into whatever it wraps.
        inner = next(
            (
                c
                for c in node.children
                if c.type
                in ("function_definition", "class_specifier", "struct_specifier", "declaration")
            ),
            None,
        )
        if inner is not None:
            prefix = node_text(node.child_by_field_name("parameters")) or ""
            _walk_one(inner, scope, symbols, imports, calls, template_prefix=f"template{prefix} ")
        return

    _walk_one(node, scope, symbols, imports, calls, template_prefix="")


def _walk_one(
    node: Node,
    scope: _Scope,
    symbols: list[ParsedSymbol],
    imports: list[ParsedImport],
    calls: list[ParsedCall],
    *,
    template_prefix: str,
) -> None:
    if node.type in ("class_specifier", "struct_specifier"):
        name_node = node.child_by_field_name("name")
        name = node_text(name_node)
        if not name:
            return  # anonymous class/struct — nothing reliable to name it by
        kind = SymbolKind.CLASS if node.type == "class_specifier" else SymbolKind.STRUCT
        qualified_name = _qualify(scope.qualified_name, name)
        keyword = node.type.split("_")[0]
        symbols.append(
            _symbol(
                name,
                qualified_name,
                kind,
                node,
                scope.qualified_name,
                f"{template_prefix}{keyword} {name}",
            )
        )
        body = node.child_by_field_name("body")
        if body is not None:
            child_scope = _Scope(qualified_name, kind, scope.caller)
            for child in body.children:
                _walk(child, child_scope, symbols, imports, calls)
        return

    if node.type == "enum_specifier":
        name_node = node.child_by_field_name("name")
        name = node_text(name_node)
        if not name:
            return
        qualified_name = _qualify(scope.qualified_name, name)
        symbols.append(
            _symbol(
                name, qualified_name, SymbolKind.ENUM, node, scope.qualified_name, f"enum {name}"
            )
        )
        return

    if node.type == "function_definition":
        _extract_function(
            node, scope, symbols, imports, calls, template_prefix=template_prefix, has_body=True
        )
        return

    if node.type in ("field_declaration", "declaration") and scope.kind in _CONTAINER_KINDS:
        # In-class method *declaration* (an inline body is a separate,
        # nested `function_definition`, already handled above).
        declarator = node.child_by_field_name("declarator")
        func_declarator = _find(declarator, "function_declarator") if declarator else None
        if func_declarator is not None:
            _extract_function(
                node,
                scope,
                symbols,
                imports,
                calls,
                template_prefix=template_prefix,
                has_body=False,
            )
        return

    for child in node.children:
        _walk(child, scope, symbols, imports, calls)


def _extract_function(
    node: Node,
    scope: _Scope,
    symbols: list[ParsedSymbol],
    imports: list[ParsedImport],
    calls: list[ParsedCall],
    *,
    template_prefix: str,
    has_body: bool,
) -> None:
    declarator = node.child_by_field_name("declarator")
    func_declarator = _find(declarator, "function_declarator") if declarator else None
    if func_declarator is None:
        return
    name_node = func_declarator.child_by_field_name("declarator")
    if name_node is None:
        return

    if name_node.type == "qualified_identifier":
        # Out-of-class definition, e.g. `Cache::get` or `Cache::~Cache`.
        local_qualified = node_text(name_node)
        name = local_qualified.rsplit("::", 1)[-1]
        kind = SymbolKind.METHOD
    else:
        name = node_text(name_node)
        local_qualified = name
        kind = SymbolKind.METHOD if scope.kind in _CONTAINER_KINDS else SymbolKind.FUNCTION

    if not name:
        return

    qualified_name = _qualify(scope.qualified_name, local_qualified)

    body = node.child_by_field_name("body")
    header_end = body.start_byte if (has_body and body is not None) else node.end_byte
    header = (
        (node.text or b"")[: header_end - node.start_byte]
        .decode("utf-8", errors="replace")
        .rstrip()
        .rstrip(";")
        .strip()
    )

    symbols.append(
        ParsedSymbol(
            name=name,
            qualified_name=qualified_name,
            kind=kind,
            span=span_of(node),
            signature=f"{template_prefix}{header}",
            parent_qualified_name=scope.qualified_name,
            visibility="definition" if has_body else "declaration",
            content_hash=content_hash(node.text or b""),
        )
    )

    if has_body and body is not None:
        child_scope = _Scope(qualified_name, SymbolKind.FUNCTION, qualified_name)
        _walk(body, child_scope, symbols, imports, calls)


def _symbol(
    name: str,
    qualified_name: str,
    kind: SymbolKind,
    node: Node,
    parent_qualified_name: str | None,
    signature: str,
) -> ParsedSymbol:
    return ParsedSymbol(
        name=name,
        qualified_name=qualified_name,
        kind=kind,
        span=span_of(node),
        signature=signature,
        parent_qualified_name=parent_qualified_name,
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


def _callee_name(function_node: Node | None) -> str | None:
    if function_node is None:
        return None
    if function_node.type == "identifier":
        return node_text(function_node)
    if function_node.type == "field_expression":
        return node_text(function_node.child_by_field_name("field"))
    if function_node.type == "qualified_identifier":
        return node_text(function_node.child_by_field_name("name"))
    return None


def _find(node: Node, type_name: str) -> Node | None:
    if node.type == type_name:
        return node
    for child in node.children:
        found = _find(child, type_name)
        if found is not None:
            return found
    return None
