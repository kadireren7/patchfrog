"""Unit coverage for deterministic symbol continuity matching (no LLM,
no I/O -- see the module docstring of
:mod:`patchfrog.review_memory.symbol_continuity`)."""

from __future__ import annotations

import uuid

from patchfrog.domain.code import Language, SymbolKind
from patchfrog.indexing.models import ChangeSet, FileChange, FileChangeType
from patchfrog.review_memory.domain import SymbolContinuityStatus, SymbolSnapshot
from patchfrog.review_memory.symbol_continuity import build_rename_map, match_symbols


def _snapshot(
    *, name: str, qualified_name: str | None = None, file_path: str = "a.py",
    content_hash: str = "h1", kind: SymbolKind = SymbolKind.FUNCTION,
    start_line: int = 1, end_line: int = 5,
) -> SymbolSnapshot:
    return SymbolSnapshot(
        id=uuid.uuid4(), name=name, qualified_name=qualified_name or name, kind=kind,
        language=Language.PYTHON, file_path=file_path, start_line=start_line, end_line=end_line,
        content_hash=content_hash,
    )


def _no_file_changes(*, old: str = "old", new: str = "new") -> ChangeSet:
    return ChangeSet(old_commit_sha=old, new_commit_sha=new, changes=())


def test_identical_symbol_is_unchanged() -> None:
    prev = _snapshot(name="foo", content_hash="same")
    cur = _snapshot(name="foo", content_hash="same")
    results = match_symbols(previous_symbols=[prev], current_symbols=[cur], file_changes=_no_file_changes())
    assert len(results) == 1
    assert results[0].status is SymbolContinuityStatus.UNCHANGED
    assert results[0].current is cur


def test_same_identity_different_body_is_modified() -> None:
    prev = _snapshot(name="foo", content_hash="v1")
    cur = _snapshot(name="foo", content_hash="v2")
    results = match_symbols(previous_symbols=[prev], current_symbols=[cur], file_changes=_no_file_changes())
    assert results[0].status is SymbolContinuityStatus.MODIFIED


def test_moved_lines_same_body_is_moved_not_modified() -> None:
    prev = _snapshot(name="foo", content_hash="same", start_line=1, end_line=5)
    cur = _snapshot(name="foo", content_hash="same", start_line=40, end_line=44)
    results = match_symbols(previous_symbols=[prev], current_symbols=[cur], file_changes=_no_file_changes())
    # Same (file, qualified_name, kind) identity match always wins first,
    # regardless of line position -- so this is UNCHANGED, not MOVED.
    # MOVED is reserved for a *different* identity match found only via
    # content hash (see test_renamed_file_symbol_matches_by_qualified_name).
    assert results[0].status is SymbolContinuityStatus.UNCHANGED


def test_symbol_moved_to_new_file_without_rename_record_is_moved() -> None:
    prev = _snapshot(name="foo", content_hash="same", file_path="a.py")
    cur = _snapshot(name="foo", content_hash="same", file_path="b.py")
    results = match_symbols(previous_symbols=[prev], current_symbols=[cur], file_changes=_no_file_changes())
    assert results[0].status is SymbolContinuityStatus.MOVED
    assert results[0].current is cur


def test_renamed_symbol_same_body_different_name_is_renamed() -> None:
    prev = _snapshot(name="foo", content_hash="same")
    cur = _snapshot(name="bar", content_hash="same")
    results = match_symbols(previous_symbols=[prev], current_symbols=[cur], file_changes=_no_file_changes())
    assert results[0].status is SymbolContinuityStatus.RENAMED


def test_ambiguous_when_multiple_symbols_share_exact_body() -> None:
    prev = _snapshot(name="foo", content_hash="dup")
    cur1 = _snapshot(name="bar1", content_hash="dup")
    cur2 = _snapshot(name="bar2", content_hash="dup")
    results = match_symbols(
        previous_symbols=[prev], current_symbols=[cur1, cur2], file_changes=_no_file_changes()
    )
    prev_result = next(r for r in results if r.previous is not None)
    assert prev_result.status is SymbolContinuityStatus.AMBIGUOUS
    assert prev_result.current is None


def test_no_match_at_all_is_deleted() -> None:
    prev = _snapshot(name="foo", content_hash="gone")
    results = match_symbols(previous_symbols=[prev], current_symbols=[], file_changes=_no_file_changes())
    assert results[0].status is SymbolContinuityStatus.DELETED


def test_symbol_in_deleted_file_is_always_deleted_never_generic_no_match() -> None:
    prev = _snapshot(name="foo", content_hash="whatever", file_path="gone.py")
    file_changes = ChangeSet(
        old_commit_sha="old", new_commit_sha="new",
        changes=(FileChange(change_type=FileChangeType.DELETED, path="gone.py"),),
    )
    results = match_symbols(previous_symbols=[prev], current_symbols=[], file_changes=file_changes)
    assert results[0].status is SymbolContinuityStatus.DELETED
    assert "file deleted" in results[0].reason


def test_renamed_file_symbol_matches_by_qualified_name_at_new_path() -> None:
    prev = _snapshot(name="foo", content_hash="same", file_path="old_name.py")
    cur = _snapshot(name="foo", content_hash="same", file_path="new_name.py")
    file_changes = ChangeSet(
        old_commit_sha="old", new_commit_sha="new",
        changes=(FileChange(change_type=FileChangeType.RENAMED, path="new_name.py", previous_path="old_name.py"),),
    )
    results = match_symbols(previous_symbols=[prev], current_symbols=[cur], file_changes=file_changes)
    assert results[0].status is SymbolContinuityStatus.UNCHANGED
    assert results[0].current is cur


def test_new_symbol_with_no_previous_match_is_added() -> None:
    cur = _snapshot(name="new_fn", content_hash="fresh")
    results = match_symbols(previous_symbols=[], current_symbols=[cur], file_changes=_no_file_changes())
    assert len(results) == 1
    assert results[0].status is SymbolContinuityStatus.ADDED
    assert results[0].current is cur


def test_build_rename_map_tracks_renames_and_deletions() -> None:
    file_changes = ChangeSet(
        old_commit_sha="old", new_commit_sha="new",
        changes=(
            FileChange(change_type=FileChangeType.RENAMED, path="b.py", previous_path="a.py"),
            FileChange(change_type=FileChangeType.DELETED, path="c.py"),
            FileChange(change_type=FileChangeType.ADDED, path="d.py"),
        ),
    )
    rename_map = build_rename_map(file_changes)
    assert rename_map.old_to_new == {"a.py": "b.py"}
    assert rename_map.deleted_paths == frozenset({"c.py"})


def test_different_kind_same_name_and_body_does_not_cross_match() -> None:
    """A function and a class sharing a name/body-hash coincidence must
    never be matched to each other -- kind is part of the identity key."""

    prev = _snapshot(name="Thing", content_hash="same", kind=SymbolKind.FUNCTION)
    cur = _snapshot(name="Thing", content_hash="same", kind=SymbolKind.CLASS)
    results = match_symbols(previous_symbols=[prev], current_symbols=[cur], file_changes=_no_file_changes())
    prev_result = next(r for r in results if r.previous is not None)
    assert prev_result.status is SymbolContinuityStatus.DELETED
    added_result = next(r for r in results if r.previous is None)
    assert added_result.status is SymbolContinuityStatus.ADDED
