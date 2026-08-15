from __future__ import annotations

from patchfrog.domain.code import ImportKind, SymbolKind
from patchfrog.parsing.python import PythonParser

PARSER = PythonParser()


def _parse(source: str, path: str = "module.py"):  # type: ignore[no-untyped-def]
    return PARSER.parse_file(relative_path=path, content=source.encode())


def test_extracts_functions_and_async_functions() -> None:
    parsed = _parse(
        "def sync_fn():\n    pass\n\n\nasync def async_fn():\n    pass\n"
    )
    names = {s.qualified_name: s.kind for s in parsed.symbols}
    assert names["sync_fn"] is SymbolKind.FUNCTION
    assert names["async_fn"] is SymbolKind.FUNCTION


def test_extracts_class_and_methods_with_correct_kinds() -> None:
    parsed = _parse(
        "class Cache:\n"
        "    def get(self, key):\n"
        "        return key\n"
    )
    by_qualified = {s.qualified_name: s for s in parsed.symbols}
    assert by_qualified["Cache"].kind is SymbolKind.CLASS
    assert by_qualified["Cache.get"].kind is SymbolKind.METHOD
    assert by_qualified["Cache.get"].parent_qualified_name == "Cache"


def test_extracts_nested_functions_with_qualified_name() -> None:
    parsed = _parse("def outer():\n    def inner():\n        pass\n    return inner\n")
    qualified_names = {s.qualified_name for s in parsed.symbols}
    assert "outer" in qualified_names
    assert "outer.inner" in qualified_names


def test_signature_includes_decorators() -> None:
    parsed = _parse("@staticmethod\ndef fn(x: int) -> int:\n    return x\n")
    fn = next(s for s in parsed.symbols if s.qualified_name == "fn")
    assert fn.signature is not None
    assert "@staticmethod" in fn.signature
    assert "def fn(x: int) -> int" in fn.signature


def test_extracts_plain_and_from_imports() -> None:
    parsed = _parse(
        "import os\n"
        "import os.path as osp\n"
        "from collections import OrderedDict\n"
        "from . import sibling\n"
    )
    targets = {(i.target, i.kind) for i in parsed.imports}
    assert ("os", ImportKind.EXTERNAL) in targets
    assert ("os.path", ImportKind.EXTERNAL) in targets
    assert ("collections.OrderedDict", ImportKind.EXTERNAL) in targets
    assert (".sibling", ImportKind.LOCAL) in targets


def test_extracts_call_sites_with_caller() -> None:
    parsed = _parse(
        "def outer():\n"
        "    return helper(1)\n"
    )
    call = next(c for c in parsed.calls if c.callee_name == "helper")
    assert call.caller_qualified_name == "outer"


def test_module_level_call_has_no_caller() -> None:
    parsed = _parse("run()\n")
    call = next(c for c in parsed.calls if c.callee_name == "run")
    assert call.caller_qualified_name is None


def test_method_call_via_attribute_access() -> None:
    parsed = _parse(
        "class Cache:\n"
        "    def get(self):\n"
        "        return self.helper()\n"
    )
    call = next(c for c in parsed.calls if c.callee_name == "helper")
    assert call.caller_qualified_name == "Cache.get"


def test_syntax_error_is_recorded_but_partial_structure_survives() -> None:
    parsed = _parse("def broken(:\n    pass\n")
    assert parsed.parse_errors
    assert "syntax error" in parsed.parse_errors[0]


def test_valid_file_has_no_parse_errors() -> None:
    parsed = _parse("def fn():\n    pass\n")
    assert parsed.parse_errors == ()
