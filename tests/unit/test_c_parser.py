from __future__ import annotations

from patchfrog.domain.code import ImportKind, SymbolKind
from patchfrog.parsing.c import CParser

PARSER = CParser()


def _parse(source: str, path: str = "module.c"):  # type: ignore[no-untyped-def]
    return PARSER.parse_file(relative_path=path, content=source.encode())


def test_extracts_function_definition() -> None:
    parsed = _parse("int add(int a, int b)\n{\n    return a + b;\n}\n")
    fn = next(s for s in parsed.symbols if s.name == "add")
    assert fn.kind is SymbolKind.FUNCTION
    assert fn.visibility == "definition"
    assert "int add(int a, int b)" in (fn.signature or "")


def test_extracts_function_prototype_declaration() -> None:
    parsed = _parse("int add(int a, int b);\n")
    fn = next(s for s in parsed.symbols if s.name == "add")
    assert fn.visibility == "declaration"


def test_extracts_struct_definition() -> None:
    parsed = _parse("struct point\n{\n    int x;\n    int y;\n};\n")
    struct = next(s for s in parsed.symbols if s.name == "point")
    assert struct.kind is SymbolKind.STRUCT


def test_extracts_typedef_struct_combo() -> None:
    parsed = _parse(
        "typedef struct s_node\n"
        "{\n"
        "    void *content;\n"
        "    struct s_node *next;\n"
        "} t_node;\n"
    )
    by_name = {s.name: s for s in parsed.symbols}
    assert by_name["s_node"].kind is SymbolKind.STRUCT
    assert by_name["t_node"].kind is SymbolKind.TYPE_ALIAS


def test_extracts_enum() -> None:
    parsed = _parse("enum color { RED, GREEN, BLUE };\n")
    enum = next(s for s in parsed.symbols if s.name == "color")
    assert enum.kind is SymbolKind.ENUM


def test_extracts_macro() -> None:
    parsed = _parse("#define MAX_SIZE 128\n")
    macro = next(s for s in parsed.symbols if s.name == "MAX_SIZE")
    assert macro.kind is SymbolKind.MACRO


def test_extracts_local_and_system_includes() -> None:
    parsed = _parse('#include "node.h"\n#include <stdlib.h>\n')
    by_target = {i.target: i.kind for i in parsed.imports}
    assert by_target["node.h"] is ImportKind.LOCAL
    assert by_target["stdlib.h"] is ImportKind.EXTERNAL


def test_extracts_call_site_with_caller() -> None:
    parsed = _parse(
        "int helper(void);\n"
        "int main(void)\n"
        "{\n"
        "    return helper();\n"
        "}\n"
    )
    call = next(c for c in parsed.calls if c.callee_name == "helper")
    assert call.caller_qualified_name == "main"


def test_extracts_global_variable() -> None:
    parsed = _parse("int global_counter = 0;\n")
    var = next(s for s in parsed.symbols if s.name == "global_counter")
    assert var.kind is SymbolKind.VARIABLE


def test_extracts_symbols_inside_include_guard() -> None:
    parsed = _parse(
        "#ifndef NODE_H\n"
        "#define NODE_H\n"
        "\n"
        "typedef struct s_node\n"
        "{\n"
        "    void *content;\n"
        "} t_node;\n"
        "\n"
        "t_node *node_new(void *content);\n"
        "\n"
        "#endif\n",
        path="node.h",
    )
    names = {s.name for s in parsed.symbols}
    # NODE_H is the include-guard macro itself — also a real symbol.
    assert names == {"NODE_H", "s_node", "t_node", "node_new"}


def test_syntax_error_is_recorded() -> None:
    parsed = _parse("int broken( {\n")
    assert parsed.parse_errors
