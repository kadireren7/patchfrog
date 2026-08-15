"""Python parser.

Extracts modules (implicit — one per file), classes, functions (including
``async def``), methods, and nested functions, with decorators captured
in the signature text. Extracts ``import``/``from ... import`` statements
and call sites.

Import classification is syntax-only: explicit relative imports
(``from . import x``) are marked local; every absolute import
(``import x``, ``from x import y``) is marked external here, *not*
because it can't be a same-repo import (`import mypackage.sub` very
often is), but because deciding that requires knowing the repository's
package layout — which is exactly what
:mod:`patchfrog.intelligence.resolution` has and this parser does not.
Module-level and class-body-level statements are not treated as call
"sites" with a meaningful caller — only calls inside a function/method
body get a ``caller_qualified_name``.
"""

from __future__ import annotations

from dataclasses import dataclass

import tree_sitter_python as tspython
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
from patchfrog.parsing.base import content_hash, node_text, qualify, span_of

_PY_LANGUAGE = TSLanguage(tspython.language())


@dataclass(frozen=True, slots=True)
class _Scope:
    qualified_name: str | None
    kind: SymbolKind | None
    caller: str | None


class PythonParser:
    language = Language.PYTHON

    def __init__(self) -> None:
        self._parser = Parser(_PY_LANGUAGE)

    def parse_file(self, *, relative_path: str, content: bytes) -> ParsedFile:
        tree = self._parser.parse(content)
        root = tree.root_node

        symbols: list[ParsedSymbol] = []
        imports: list[ParsedImport] = []
        calls: list[ParsedCall] = []
        errors: list[str] = []

        if root.has_error:
            errors.append("syntax error(s) present; structure may be incomplete")

        _walk(root, content, _Scope(None, None, None), symbols, imports, calls)

        return ParsedFile(
            path=relative_path,
            language=Language.PYTHON,
            symbols=tuple(symbols),
            imports=tuple(imports),
            calls=tuple(calls),
            parse_errors=tuple(errors),
        )


def _walk(
    node: Node,
    content: bytes,
    scope: _Scope,
    symbols: list[ParsedSymbol],
    imports: list[ParsedImport],
    calls: list[ParsedCall],
) -> None:
    if node.type == "import_statement":
        imports.extend(_extract_plain_imports(node))
        return

    if node.type == "import_from_statement":
        imports.extend(_extract_from_imports(node))
        return

    if node.type == "class_definition":
        name_node = node.child_by_field_name("name")
        name = node_text(name_node)
        qualified_name = qualify(scope.qualified_name, name)
        symbols.append(
            ParsedSymbol(
                name=name,
                qualified_name=qualified_name,
                kind=SymbolKind.CLASS,
                span=span_of(node),
                signature=f"class {name}",
                parent_qualified_name=scope.qualified_name,
                visibility=None,
                content_hash=content_hash(node.text or b""),
            )
        )
        body = node.child_by_field_name("body")
        child_scope = _Scope(qualified_name, SymbolKind.CLASS, scope.caller)
        if body is not None:
            _walk(body, content, child_scope, symbols, imports, calls)
        return

    if node.type == "function_definition":
        name_node = node.child_by_field_name("name")
        name = node_text(name_node)
        qualified_name = qualify(scope.qualified_name, name)
        kind = SymbolKind.METHOD if scope.kind is SymbolKind.CLASS else SymbolKind.FUNCTION
        symbols.append(
            ParsedSymbol(
                name=name,
                qualified_name=qualified_name,
                kind=kind,
                span=span_of(node),
                signature=_function_signature(node),
                parent_qualified_name=scope.qualified_name,
                visibility=None,
                content_hash=content_hash(node.text or b""),
            )
        )
        body = node.child_by_field_name("body")
        child_scope = _Scope(qualified_name, SymbolKind.FUNCTION, qualified_name)
        if body is not None:
            _walk(body, content, child_scope, symbols, imports, calls)
        return

    if node.type == "call":
        callee_name = _callee_name(node.child_by_field_name("function"))
        if callee_name:
            calls.append(
                ParsedCall(
                    callee_name=callee_name,
                    caller_qualified_name=scope.caller,
                    line=node.start_point[0] + 1,
                    column=node.start_point[1],
                )
            )
        # Calls can nest inside their own arguments (`foo(bar())`).
        for child in node.children:
            _walk(child, content, scope, symbols, imports, calls)
        return

    for child in node.children:
        _walk(child, content, scope, symbols, imports, calls)


def _callee_name(function_node: Node | None) -> str | None:
    if function_node is None:
        return None
    if function_node.type == "identifier":
        return node_text(function_node)
    if function_node.type == "attribute":
        return node_text(function_node.child_by_field_name("attribute"))
    return None


def _function_signature(node: Node) -> str:
    """The `def ...(...):` header text, with any decorators prefixed."""

    colon = next((c for c in node.children if c.type == ":"), None)
    header_end = colon.end_byte if colon is not None else node.end_byte
    header = (node.text or b"")[: header_end - node.start_byte].decode("utf-8", errors="replace")
    header = header.rstrip().removesuffix(":").strip()

    decorators: list[str] = []
    parent = node.parent
    if parent is not None and parent.type == "decorated_definition":
        for child in parent.children:
            if child.type == "decorator":
                decorators.append(node_text(child))

    return "\n".join([*decorators, header]) if decorators else header


def _extract_plain_imports(node: Node) -> list[ParsedImport]:
    line = node.start_point[0] + 1
    results: list[ParsedImport] = []
    for child in node.children:
        if child.type == "dotted_name":
            results.append(
                ParsedImport(
                    raw_text=node_text(node), target=node_text(child), kind=ImportKind.EXTERNAL, line=line
                )
            )
        elif child.type == "aliased_import":
            dotted = child.child_by_field_name("name")
            if dotted is not None:
                results.append(
                    ParsedImport(
                        raw_text=node_text(node),
                        target=node_text(dotted),
                        kind=ImportKind.EXTERNAL,
                        line=line,
                    )
                )
    return results


def _extract_from_imports(node: Node) -> list[ParsedImport]:
    line = node.start_point[0] + 1
    raw_text = node_text(node)

    module_node = node.child_by_field_name("module_name")
    is_relative = module_node is not None and module_node.type == "relative_import"
    module_text = node_text(module_node)
    module_node_id = module_node.id if module_node is not None else None

    results: list[ParsedImport] = []
    for child in node.children:
        name_text: str | None = None
        if child.type == "dotted_name" and child.id != module_node_id:
            name_text = node_text(child)
        elif child.type == "aliased_import":
            dotted = child.child_by_field_name("name")
            name_text = node_text(dotted) if dotted is not None else None
        elif child.type == "wildcard_import":
            name_text = "*"

        if name_text is None:
            continue

        if not module_text:
            target = name_text
        elif module_text.endswith("."):
            target = f"{module_text}{name_text}"
        else:
            target = f"{module_text}.{name_text}"
        results.append(
            ParsedImport(
                raw_text=raw_text,
                target=target,
                kind=ImportKind.LOCAL if is_relative else ImportKind.EXTERNAL,
                line=line,
            )
        )
    return results
