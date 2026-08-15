from __future__ import annotations

from patchfrog.analysis.changed_lines import build_changed_lines_by_file, classify
from patchfrog.diff.models import DiffFile, DiffHunk, DiffLine, DiffLineType
from patchfrog.domain.code import SourceSpan


def _diff_file(path: str, added_new_lines: list[int]) -> DiffFile:
    lines = tuple(
        DiffLine(line_type=DiffLineType.ADDITION, old_line_number=None, new_line_number=n, content="x")
        for n in added_new_lines
    )
    hunk = DiffHunk(old_start=1, old_lines=0, new_start=1, new_lines=len(lines), section_heading=None, lines=lines)
    return DiffFile(path=path, hunks=(hunk,))


def test_build_changed_lines_by_file_collects_only_additions() -> None:
    diff_files = [_diff_file("cache.c", [80, 81, 90])]

    result = build_changed_lines_by_file(diff_files)

    assert result == {"cache.c": frozenset({80, 81, 90})}


def test_finding_exactly_on_an_added_line_is_on_changed_line() -> None:
    changed = {"cache.c": frozenset({91})}

    result = classify(
        file_path="cache.c",
        span=SourceSpan(91, 91, 1, 5),
        changed_lines_by_file=changed,
        containing_symbol_span=None,
    )

    assert result.is_on_changed_line is True


def test_finding_on_unchanged_context_line_is_not_on_changed_line() -> None:
    changed = {"cache.c": frozenset({91})}

    result = classify(
        file_path="cache.c",
        span=SourceSpan(50, 50, 1, 5),
        changed_lines_by_file=changed,
        containing_symbol_span=None,
    )

    assert result.is_on_changed_line is False


def test_finding_in_changed_symbol_but_outside_changed_lines() -> None:
    changed = {"cache.c": frozenset({91})}
    # symbol spans lines 80-95, the finding itself is at line 84 (not 91)
    symbol_span = SourceSpan(80, 95, 0, 0)

    result = classify(
        file_path="cache.c",
        span=SourceSpan(84, 84, 1, 5),
        changed_lines_by_file=changed,
        containing_symbol_span=symbol_span,
    )

    assert result.is_on_changed_line is False
    assert result.is_in_changed_symbol is True


def test_finding_in_unchanged_symbol_and_unchanged_lines() -> None:
    changed = {"cache.c": frozenset({200})}
    symbol_span = SourceSpan(80, 95, 0, 0)

    result = classify(
        file_path="cache.c",
        span=SourceSpan(84, 84, 1, 5),
        changed_lines_by_file=changed,
        containing_symbol_span=symbol_span,
    )

    assert result.is_on_changed_line is False
    assert result.is_in_changed_symbol is False


def test_deleted_line_only_diff_produces_no_changed_lines_for_new_file_numbers() -> None:
    hunk = DiffHunk(
        old_start=10, old_lines=1, new_start=10, new_lines=0, section_heading=None,
        lines=(DiffLine(line_type=DiffLineType.DELETION, old_line_number=10, new_line_number=None, content="x"),),
    )
    diff_file = DiffFile(path="cache.c", hunks=(hunk,))

    result = build_changed_lines_by_file([diff_file])

    assert result == {"cache.c": frozenset()}


def test_finding_in_a_file_with_no_diff_entry_is_never_changed() -> None:
    result = classify(
        file_path="untouched.c",
        span=SourceSpan(1, 1, 1, 1),
        changed_lines_by_file={},
        containing_symbol_span=None,
    )

    assert result.is_on_changed_line is False
    assert result.is_in_changed_symbol is False
