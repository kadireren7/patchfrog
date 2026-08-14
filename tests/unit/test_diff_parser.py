from __future__ import annotations

import pytest

from patchfrog.diff.models import DiffLineType
from patchfrog.diff.parser import PatchParseError, build_diff_file, parse_patch

SINGLE_HUNK_PATCH = (
    "@@ -10,4 +10,5 @@\n"
    " int foo(void)\n"
    " {\n"
    "-    return (0);\n"
    "+    int result = 42;\n"
    "+    return (result);\n"
    " }"
)


def test_single_hunk_matches_spec_example() -> None:
    hunks = parse_patch(SINGLE_HUNK_PATCH)

    assert len(hunks) == 1
    hunk = hunks[0]
    assert (hunk.old_start, hunk.old_lines, hunk.new_start, hunk.new_lines) == (10, 4, 10, 5)

    expected = [
        (DiffLineType.CONTEXT, 10, 10),
        (DiffLineType.CONTEXT, 11, 11),
        (DiffLineType.DELETION, 12, None),
        (DiffLineType.ADDITION, None, 12),
        (DiffLineType.ADDITION, None, 13),
        (DiffLineType.CONTEXT, 13, 14),
    ]
    actual = [(line.line_type, line.old_line_number, line.new_line_number) for line in hunk.lines]
    assert actual == expected


def test_multiple_hunks() -> None:
    patch = (
        "@@ -1,2 +1,2 @@\n"
        "-old first line\n"
        "+new first line\n"
        " second line\n"
        "@@ -20,2 +20,3 @@\n"
        " context\n"
        "+an addition far below\n"
        " more context"
    )

    hunks = parse_patch(patch)

    assert len(hunks) == 2
    assert hunks[0].old_start == 1
    assert hunks[1].old_start == 20
    assert hunks[1].lines[1].line_type is DiffLineType.ADDITION
    assert hunks[1].lines[1].new_line_number == 21


def test_only_additions() -> None:
    patch = "@@ -0,0 +1,2 @@\n+line one\n+line two"

    hunks = parse_patch(patch)

    lines = hunks[0].lines
    assert all(line.line_type is DiffLineType.ADDITION for line in lines)
    assert [line.new_line_number for line in lines] == [1, 2]
    assert all(line.old_line_number is None for line in lines)


def test_only_deletions() -> None:
    patch = "@@ -1,2 +0,0 @@\n-line one\n-line two"

    hunks = parse_patch(patch)

    lines = hunks[0].lines
    assert all(line.line_type is DiffLineType.DELETION for line in lines)
    assert [line.old_line_number for line in lines] == [1, 2]
    assert all(line.new_line_number is None for line in lines)


def test_mixed_diff_content_preserved() -> None:
    patch = "@@ -1,1 +1,1 @@\n-old content here\n+new content here"

    hunks = parse_patch(patch)

    assert hunks[0].lines[0].content == "old content here"
    assert hunks[0].lines[1].content == "new content here"


def test_empty_patch_returns_no_hunks() -> None:
    assert parse_patch("") == ()
    assert parse_patch(None) == ()


def test_binary_or_missing_patch_produces_empty_diff_file() -> None:
    diff_file = build_diff_file("image.png", None)

    assert diff_file.path == "image.png"
    assert diff_file.hunks == ()
    assert diff_file.added_lines == []


def test_missing_newline_marker_is_ignored_not_content() -> None:
    patch = "@@ -1,1 +1,1 @@\n-old\n+new\n\\ No newline at end of file"

    hunks = parse_patch(patch)

    assert len(hunks[0].lines) == 2
    assert hunks[0].lines[-1].content == "new"


def test_malformed_hunk_header_raises() -> None:
    with pytest.raises(PatchParseError):
        parse_patch("@@ not a real header @@\n+something")


def test_content_line_outside_hunk_raises() -> None:
    with pytest.raises(PatchParseError):
        parse_patch("+ a line with no preceding hunk header")


def test_added_and_deleted_lines_helpers() -> None:
    diff_file = build_diff_file("foo.c", SINGLE_HUNK_PATCH)

    assert [line.content for line in diff_file.added_lines] == [
        "    int result = 42;",
        "    return (result);",
    ]
    assert [line.content for line in diff_file.deleted_lines] == ["    return (0);"]
