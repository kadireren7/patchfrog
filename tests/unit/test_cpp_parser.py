from __future__ import annotations

from patchfrog.domain.code import ImportKind, SymbolKind
from patchfrog.parsing.cpp import CppParser

PARSER = CppParser()


def _parse(source: str, path: str = "module.cpp"):  # type: ignore[no-untyped-def]
    return PARSER.parse_file(relative_path=path, content=source.encode())


def test_extracts_namespace_qualified_class_and_method() -> None:
    parsed = _parse(
        "namespace patchfrog {\n"
        "class Cache {\n"
        "public:\n"
        "    int get(int key);\n"
        "};\n"
        "}\n"
    )
    by_qualified = {s.qualified_name: s for s in parsed.symbols}
    assert by_qualified["patchfrog::Cache"].kind is SymbolKind.CLASS
    assert by_qualified["patchfrog::Cache::get"].kind is SymbolKind.METHOD
    assert by_qualified["patchfrog::Cache::get"].visibility == "declaration"


def test_extracts_out_of_class_method_definition_qualified() -> None:
    parsed = _parse(
        "namespace ns {\n"
        "class Cache {\n"
        "public:\n"
        "    int get(int key);\n"
        "};\n"
        "int Cache::get(int key) { return key; }\n"
        "}\n"
    )
    definition = next(
        s for s in parsed.symbols if s.qualified_name == "ns::Cache::get" and s.visibility == "definition"
    )
    assert definition.kind is SymbolKind.METHOD


def test_extracts_constructor_and_destructor_as_methods() -> None:
    parsed = _parse(
        "class Widget {\n"
        "public:\n"
        "    Widget();\n"
        "    ~Widget();\n"
        "};\n"
    )
    names = {s.name for s in parsed.symbols if s.kind is SymbolKind.METHOD}
    assert "Widget" in names
    assert "~Widget" in names


def test_extracts_struct_and_enum() -> None:
    parsed = _parse("struct Point { int x; int y; };\nenum Color { RED, GREEN };\n")
    by_name = {s.name: s.kind for s in parsed.symbols}
    assert by_name["Point"] is SymbolKind.STRUCT
    assert by_name["Color"] is SymbolKind.ENUM


def test_extracts_template_function_with_prefix() -> None:
    parsed = _parse("template<typename T>\nT identity(T value) { return value; }\n")
    fn = next(s for s in parsed.symbols if s.name == "identity")
    assert fn.signature is not None
    assert fn.signature.startswith("template<typename T>")


def test_extracts_includes() -> None:
    parsed = _parse('#include "cache.hpp"\n#include <vector>\n')
    by_target = {i.target: i.kind for i in parsed.imports}
    assert by_target["cache.hpp"] is ImportKind.LOCAL
    assert by_target["vector"] is ImportKind.EXTERNAL


def test_extracts_call_site_inside_method_with_qualified_caller() -> None:
    parsed = _parse(
        "namespace ns {\n"
        "class Cache {\n"
        "public:\n"
        "    int get(int key) { return helper(key); }\n"
        "};\n"
        "}\n"
    )
    call = next(c for c in parsed.calls if c.callee_name == "helper")
    assert call.caller_qualified_name == "ns::Cache::get"


def test_syntax_error_is_recorded() -> None:
    parsed = _parse("class Broken {\n")
    assert parsed.parse_errors
