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
from patchfrog.indexing.parse_cache import deserialize_parsed_file, serialize_parsed_file


def test_round_trip_preserves_symbols_imports_and_calls() -> None:
    original = ParsedFile(
        path="original/path.py",
        language=Language.PYTHON,
        symbols=(
            ParsedSymbol(
                name="Cache", qualified_name="Cache", kind=SymbolKind.CLASS,
                span=SourceSpan(1, 5, 0, 10), signature="class Cache", parent_qualified_name=None,
                visibility=None, content_hash="abc",
            ),
        ),
        imports=(ParsedImport(raw_text="import os", target="os", kind=ImportKind.EXTERNAL, line=1),),
        calls=(ParsedCall(callee_name="helper", caller_qualified_name="Cache.get", line=3, column=4),),
        parse_errors=("syntax error(s) present; structure may be incomplete",),
    )

    payload = serialize_parsed_file(original)
    restored = deserialize_parsed_file(
        relative_path="different/path.py", language=Language.PYTHON, payload=payload
    )

    # Path is intentionally not part of the cache key/content — it's
    # supplied fresh by the caller for whichever file the content was
    # found under this run.
    assert restored.path == "different/path.py"
    assert restored.language is Language.PYTHON
    assert restored.symbols == original.symbols
    assert restored.imports == original.imports
    assert restored.calls == original.calls
    assert restored.parse_errors == original.parse_errors


def test_round_trip_empty_file() -> None:
    original = ParsedFile(path="empty.py", language=Language.PYTHON)

    payload = serialize_parsed_file(original)
    restored = deserialize_parsed_file(relative_path="empty.py", language=Language.PYTHON, payload=payload)

    assert restored.symbols == ()
    assert restored.imports == ()
    assert restored.calls == ()
    assert restored.parse_errors == ()
