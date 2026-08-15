"""Conservative source <-> test relationship heuristics.

Two independent signals, both recorded with a human-readable reason
(never presented as semantic certainty):

- **Filename pattern**: a test file's name (``test_cache.py``,
  ``cache_test.c``, ``CacheTest.cpp``, ...) with test markers stripped
  matches exactly one non-test file's stem, anywhere in the repository.
  More than one match is ambiguous and is skipped rather than guessed.
- **Import evidence**: a test file directly imports/includes a specific
  non-test file — the strongest available signal, from actual resolved
  references rather than naming convention.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import PurePosixPath

from patchfrog.indexing.models import FileInventoryEntry
from patchfrog.intelligence.resolution import ResolvedImport

_TEST_MARKER_RE = re.compile(r"(^test[_-]?|[_-]?test$)", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class TestRelationship:
    test_file_path: str
    source_file_path: str
    reason: str


def infer_test_relationships(
    inventory: list[FileInventoryEntry],
    resolved_imports: list[ResolvedImport],
) -> list[TestRelationship]:
    test_files = [e for e in inventory if e.is_test]
    source_files = [e for e in inventory if not e.is_test]

    relationships: dict[tuple[str, str], set[str]] = {}

    _add_filename_matches(test_files, source_files, relationships)
    _add_import_evidence(test_files, resolved_imports, relationships)

    return [
        TestRelationship(test_file_path=test_path, source_file_path=source_path, reason=", ".join(sorted(reasons)))
        for (test_path, source_path), reasons in relationships.items()
    ]


def _add_filename_matches(
    test_files: list[FileInventoryEntry],
    source_files: list[FileInventoryEntry],
    relationships: dict[tuple[str, str], set[str]],
) -> None:
    stems_by_candidate: dict[str, list[str]] = {}
    for src in source_files:
        stem = PurePosixPath(src.relative_path).stem.lower()
        stems_by_candidate.setdefault(stem, []).append(src.relative_path)

    for test in test_files:
        stem = PurePosixPath(test.relative_path).stem
        candidate_stem = _TEST_MARKER_RE.sub("", stem).strip("_-").lower()
        if not candidate_stem:
            continue
        matches = stems_by_candidate.get(candidate_stem, [])
        if len(matches) == 1:
            key = (test.relative_path, matches[0])
            relationships.setdefault(key, set()).add("filename pattern")


def _add_import_evidence(
    test_files: list[FileInventoryEntry],
    resolved_imports: list[ResolvedImport],
    relationships: dict[tuple[str, str], set[str]],
) -> None:
    test_paths = {e.relative_path for e in test_files}
    for ri in resolved_imports:
        if ri.file_path not in test_paths or ri.resolved_file_path is None:
            continue
        key = (ri.file_path, ri.resolved_file_path)
        relationships.setdefault(key, set()).add("import evidence")
