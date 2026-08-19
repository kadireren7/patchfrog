"""Unit coverage for patchfrog.publishing.diff_mapper -- addition,
modification, deletion, rename, multiple hunks, line outside diff,
multiline range, missing patch, binary file. Pure/synchronous, no
network, no database."""

from __future__ import annotations

from patchfrog.diff.parser import build_diff_file
from patchfrog.domain.pull_request import ChangedFile, FileChangeStatus
from patchfrog.publishing.diff_mapper import is_safe_relative_path, map_finding_to_diff_position
from patchfrog.publishing.domain import DiffSide, UnmappableReason

_MODIFIED_PATCH = "\n".join(
    [
        "@@ -10,6 +10,8 @@ def existing():",
        " context_line_10",
        " context_line_11",
        "+added_line_12",
        "+added_line_13",
        " context_line_14",
        "-removed_line_15",
        " context_line_16",
    ]
)


def _changed_file(path: str = "src/billing.py", *, status: FileChangeStatus = FileChangeStatus.MODIFIED, patch: str | None = _MODIFIED_PATCH) -> ChangedFile:
    return ChangedFile(path=path, previous_path=None, status=status, additions=2, deletions=1, patch=patch)


def test_addition_line_maps_to_new_side() -> None:
    changed_file = _changed_file()
    diff_file = build_diff_file(changed_file.path, changed_file.patch)

    outcome = map_finding_to_diff_position(
        finding_path="src/billing.py", start_line=12, end_line=12, changed_file=changed_file, diff_file=diff_file
    )

    assert outcome.is_mappable
    assert outcome.position is not None
    assert outcome.position.side is DiffSide.NEW
    assert outcome.position.line == 12


def test_context_line_is_also_mappable() -> None:
    changed_file = _changed_file()
    diff_file = build_diff_file(changed_file.path, changed_file.patch)

    outcome = map_finding_to_diff_position(
        finding_path="src/billing.py", start_line=10, end_line=10, changed_file=changed_file, diff_file=diff_file
    )

    assert outcome.is_mappable
    assert outcome.position is not None and outcome.position.line == 10


def test_line_outside_any_hunk_is_unmappable() -> None:
    changed_file = _changed_file()
    diff_file = build_diff_file(changed_file.path, changed_file.patch)

    outcome = map_finding_to_diff_position(
        finding_path="src/billing.py", start_line=500, end_line=500, changed_file=changed_file, diff_file=diff_file
    )

    assert not outcome.is_mappable
    assert outcome.unmappable_reason is UnmappableReason.LINE_NOT_IN_DIFF


def test_unambiguous_multiline_range_within_one_hunk() -> None:
    changed_file = _changed_file()
    diff_file = build_diff_file(changed_file.path, changed_file.patch)

    outcome = map_finding_to_diff_position(
        finding_path="src/billing.py", start_line=12, end_line=13, changed_file=changed_file, diff_file=diff_file
    )

    assert outcome.is_mappable
    assert outcome.position is not None
    assert outcome.position.line == 13
    assert outcome.position.start_line == 12
    assert outcome.position.start_side is DiffSide.NEW


def test_multiline_range_partially_outside_diff_falls_back_to_end_line() -> None:
    changed_file = _changed_file()
    diff_file = build_diff_file(changed_file.path, changed_file.patch)

    # 12 is in-diff, 999 is not -- range is ambiguous, must fall back to a
    # single valid line rather than guessing.
    outcome = map_finding_to_diff_position(
        finding_path="src/billing.py", start_line=12, end_line=999, changed_file=changed_file, diff_file=diff_file
    )

    assert outcome.is_mappable
    assert outcome.position is not None
    assert outcome.position.start_line is None  # not a multi-line comment -- single fallback line
    assert outcome.position.line == 12  # end_line (999) unmappable, falls back to start_line


def test_multiline_range_fully_outside_diff_is_unmappable() -> None:
    changed_file = _changed_file()
    diff_file = build_diff_file(changed_file.path, changed_file.patch)

    outcome = map_finding_to_diff_position(
        finding_path="src/billing.py", start_line=800, end_line=900, changed_file=changed_file, diff_file=diff_file
    )

    assert not outcome.is_mappable
    assert outcome.unmappable_reason is UnmappableReason.LINE_NOT_IN_DIFF


def test_deleted_file_is_unmappable() -> None:
    changed_file = _changed_file(status=FileChangeStatus.REMOVED, patch=None)
    diff_file = build_diff_file(changed_file.path, changed_file.patch)

    outcome = map_finding_to_diff_position(
        finding_path="src/billing.py", start_line=12, end_line=12, changed_file=changed_file, diff_file=diff_file
    )

    assert not outcome.is_mappable
    assert outcome.unmappable_reason is UnmappableReason.FILE_DELETED


def test_missing_patch_binary_file_is_unmappable() -> None:
    changed_file = _changed_file(patch=None)
    diff_file = build_diff_file(changed_file.path, changed_file.patch)

    outcome = map_finding_to_diff_position(
        finding_path="src/billing.py", start_line=12, end_line=12, changed_file=changed_file, diff_file=diff_file
    )

    assert not outcome.is_mappable
    assert outcome.unmappable_reason is UnmappableReason.BINARY_OR_NO_PATCH


def test_file_not_present_in_diff_is_unmappable() -> None:
    outcome = map_finding_to_diff_position(
        finding_path="src/not_in_pr.py", start_line=1, end_line=1, changed_file=None, diff_file=None
    )

    assert not outcome.is_mappable
    assert outcome.unmappable_reason is UnmappableReason.FILE_NOT_IN_DIFF


def test_added_file_all_lines_mappable() -> None:
    patch = "@@ -0,0 +1,3 @@\n+line1\n+line2\n+line3"
    changed_file = ChangedFile(
        path="src/new_file.py", previous_path=None, status=FileChangeStatus.ADDED, additions=3, deletions=0, patch=patch
    )
    diff_file = build_diff_file(changed_file.path, changed_file.patch)

    outcome = map_finding_to_diff_position(
        finding_path="src/new_file.py", start_line=2, end_line=2, changed_file=changed_file, diff_file=diff_file
    )

    assert outcome.is_mappable
    assert outcome.position is not None and outcome.position.line == 2


def test_renamed_file_maps_using_current_path_and_new_line_numbers() -> None:
    patch = "@@ -1,3 +1,4 @@\n context\n+added_after_rename\n context\n context"
    changed_file = ChangedFile(
        path="src/renamed_to.py", previous_path="src/renamed_from.py", status=FileChangeStatus.RENAMED,
        additions=1, deletions=0, patch=patch,
    )
    diff_file = build_diff_file(changed_file.path, changed_file.patch)

    outcome = map_finding_to_diff_position(
        finding_path="src/renamed_to.py", start_line=2, end_line=2, changed_file=changed_file, diff_file=diff_file
    )

    assert outcome.is_mappable
    assert outcome.position is not None
    assert outcome.position.path == "src/renamed_to.py"


def test_finding_path_using_old_rename_name_is_unmappable() -> None:
    """A finding must exactly match the diff's own current path -- never
    silently accepted against the file's *previous* name."""

    patch = "@@ -1,3 +1,4 @@\n context\n+added_after_rename\n context\n context"
    changed_file = ChangedFile(
        path="src/renamed_to.py", previous_path="src/renamed_from.py", status=FileChangeStatus.RENAMED,
        additions=1, deletions=0, patch=patch,
    )
    diff_file = build_diff_file(changed_file.path, changed_file.patch)

    outcome = map_finding_to_diff_position(
        finding_path="src/renamed_from.py", start_line=2, end_line=2, changed_file=changed_file, diff_file=diff_file
    )

    assert not outcome.is_mappable
    assert outcome.unmappable_reason is UnmappableReason.FILE_NOT_IN_DIFF


def test_multiple_hunks_each_independently_mappable() -> None:
    patch = "\n".join(
        [
            "@@ -1,2 +1,3 @@",
            " context_a",
            "+added_in_first_hunk",
            " context_b",
            "@@ -50,2 +51,3 @@",
            " context_c",
            "+added_in_second_hunk",
            " context_d",
        ]
    )
    changed_file = _changed_file(patch=patch)
    diff_file = build_diff_file(changed_file.path, changed_file.patch)

    first = map_finding_to_diff_position(
        finding_path="src/billing.py", start_line=2, end_line=2, changed_file=changed_file, diff_file=diff_file
    )
    second = map_finding_to_diff_position(
        finding_path="src/billing.py", start_line=52, end_line=52, changed_file=changed_file, diff_file=diff_file
    )

    assert first.is_mappable and second.is_mappable


def test_multiline_range_spanning_two_hunks_is_ambiguous_falls_back() -> None:
    patch = "\n".join(
        [
            "@@ -1,2 +1,3 @@",
            " context_a",
            "+added_in_first_hunk",
            " context_b",
            "@@ -50,2 +51,3 @@",
            " context_c",
            "+added_in_second_hunk",
            " context_d",
        ]
    )
    changed_file = _changed_file(patch=patch)
    diff_file = build_diff_file(changed_file.path, changed_file.patch)

    # 2 is in the first hunk, 52 in the second -- the numeric range 2-52
    # covers plenty of lines belonging to neither, so this must never be
    # treated as one unambiguous multi-line comment.
    outcome = map_finding_to_diff_position(
        finding_path="src/billing.py", start_line=2, end_line=52, changed_file=changed_file, diff_file=diff_file
    )

    assert outcome.is_mappable
    assert outcome.position is not None
    assert outcome.position.start_line is None  # fell back to a single line, not a multi-line comment


def test_path_traversal_is_unsafe() -> None:
    assert not is_safe_relative_path("../../etc/passwd")
    assert not is_safe_relative_path("/etc/passwd")
    assert not is_safe_relative_path("src/../../etc/passwd")
    assert is_safe_relative_path("src/billing.py")


def test_unsafe_finding_path_is_unmappable_even_with_a_matching_diff_file() -> None:
    changed_file = _changed_file(path="../escape.py")
    diff_file = build_diff_file(changed_file.path, changed_file.patch)

    outcome = map_finding_to_diff_position(
        finding_path="../escape.py", start_line=12, end_line=12, changed_file=changed_file, diff_file=diff_file
    )

    assert not outcome.is_mappable
    assert outcome.unmappable_reason is UnmappableReason.PATH_UNSAFE
