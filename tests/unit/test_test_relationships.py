from __future__ import annotations

from patchfrog.domain.code import ImportKind, ParsedImport
from patchfrog.indexing.models import FileInventoryEntry
from patchfrog.intelligence.resolution import ResolvedImport
from patchfrog.intelligence.tests import infer_test_relationships


def _entry(path: str, *, is_test: bool) -> FileInventoryEntry:
    return FileInventoryEntry(
        relative_path=path, language=None, size_bytes=1, content_hash="h",
        git_blob_sha=None, is_test=is_test, is_generated=False,
    )


def test_filename_pattern_matches_unique_source() -> None:
    inventory = [_entry("tests/test_cache.py", is_test=True), _entry("src/cache.py", is_test=False)]

    relationships = infer_test_relationships(inventory, [])

    assert len(relationships) == 1
    assert relationships[0].test_file_path == "tests/test_cache.py"
    assert relationships[0].source_file_path == "src/cache.py"
    assert "filename pattern" in relationships[0].reason


def test_c_style_test_suffix_matches() -> None:
    inventory = [_entry("tests/node_test.c", is_test=True), _entry("src/node.c", is_test=False)]

    relationships = infer_test_relationships(inventory, [])

    assert relationships[0].source_file_path == "src/node.c"


def test_ambiguous_filename_match_is_skipped() -> None:
    inventory = [
        _entry("tests/test_cache.py", is_test=True),
        _entry("src/cache.py", is_test=False),
        _entry("legacy/cache.py", is_test=False),
    ]

    relationships = infer_test_relationships(inventory, [])

    assert relationships == []


def test_import_evidence_adds_relationship_independent_of_filename() -> None:
    inventory = [_entry("tests/test_thing.py", is_test=True), _entry("src/unrelated_name.py", is_test=False)]
    imp = ParsedImport(raw_text="from src.unrelated_name import fn", target="src.unrelated_name.fn", kind=ImportKind.EXTERNAL, line=1)
    resolved_imports = [
        ResolvedImport(file_path="tests/test_thing.py", import_=imp, resolved_file_path="src/unrelated_name.py")
    ]

    relationships = infer_test_relationships(inventory, resolved_imports)

    assert len(relationships) == 1
    assert relationships[0].reason == "import evidence"


def test_both_signals_combine_into_one_relationship_with_both_reasons() -> None:
    inventory = [_entry("tests/test_cache.py", is_test=True), _entry("src/cache.py", is_test=False)]
    imp = ParsedImport(raw_text="from src.cache import Cache", target="src.cache.Cache", kind=ImportKind.EXTERNAL, line=1)
    resolved_imports = [
        ResolvedImport(file_path="tests/test_cache.py", import_=imp, resolved_file_path="src/cache.py")
    ]

    relationships = infer_test_relationships(inventory, resolved_imports)

    assert len(relationships) == 1
    assert relationships[0].reason == "filename pattern, import evidence"
