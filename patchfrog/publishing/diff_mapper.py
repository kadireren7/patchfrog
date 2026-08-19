"""Deterministic, diff-aware mapping from a finding's source location to a
GitHub-commentable diff position.

This is the core Phase 6 safety mechanism: GitHub inline comments can only
be attached to a line that is actually part of the pull request's diff
(context, addition, or deletion within a hunk) -- never a merely-unchanged
line elsewhere in the file, even if a finding legitimately points there.
This module never guesses a "nearest" line; anything it cannot map with
certainty comes back :class:`Unmappable`, and callers (the planner) are
expected to preserve the finding in the review summary instead of
silently dropping it.

Pure and synchronous -- no network, no database, fully unit-testable
against :class:`~patchfrog.domain.pull_request.ChangedFile`/
:class:`~patchfrog.diff.models.DiffFile` fixtures alone.
"""

from __future__ import annotations

from dataclasses import dataclass

from patchfrog.diff.models import DiffFile, DiffLineType
from patchfrog.domain.pull_request import ChangedFile, FileChangeStatus
from patchfrog.publishing.domain import DiffSide, MappedPosition, UnmappableReason


@dataclass(frozen=True, slots=True)
class DiffMappingOutcome:
    """Either a successful :attr:`position`, or an
    :attr:`unmappable_reason` with a human-readable :attr:`detail` --
    never both, never neither."""

    position: MappedPosition | None
    unmappable_reason: UnmappableReason | None
    detail: str

    @property
    def is_mappable(self) -> bool:
        return self.position is not None


def _mappable(position: MappedPosition) -> DiffMappingOutcome:
    return DiffMappingOutcome(position=position, unmappable_reason=None, detail="mapped")


def _unmappable(reason: UnmappableReason, detail: str) -> DiffMappingOutcome:
    return DiffMappingOutcome(position=None, unmappable_reason=reason, detail=detail)


def is_safe_relative_path(path: str) -> bool:
    """Reject path traversal, absolute paths, and other confusable shapes
    before a finding's path is ever trusted enough to compare against a
    diff file's own path (see :mod:`patchfrog.publishing.diff_mapper`
    module docstring, section 51 of the Phase 6 spec: "path safety")."""

    if not path or path != path.strip():
        return False
    if path.startswith("/") or path.startswith("~"):
        return False
    if "\\" in path or "\x00" in path:
        return False
    segments = path.split("/")
    return not any(segment in ("", ".", "..") for segment in segments)


def map_finding_to_diff_position(
    *,
    finding_path: str,
    start_line: int,
    end_line: int,
    changed_file: ChangedFile | None,
    diff_file: DiffFile | None,
) -> DiffMappingOutcome:
    """Map one finding's ``[start_line, end_line]`` (inclusive, in the
    file's *current* -- i.e. head-commit -- content, exactly what Phase 5
    findings always report) to a publishable GitHub diff position, or
    explain precisely why that is not possible.
    """

    if not is_safe_relative_path(finding_path):
        return _unmappable(UnmappableReason.PATH_UNSAFE, f"unsafe path: {finding_path!r}")

    if changed_file is None or diff_file is None:
        return _unmappable(UnmappableReason.FILE_NOT_IN_DIFF, f"{finding_path!r} is not part of this PR's diff")

    if changed_file.path != finding_path:
        return _unmappable(
            UnmappableReason.FILE_NOT_IN_DIFF,
            f"finding path {finding_path!r} does not exactly match diff file path {changed_file.path!r}",
        )

    if changed_file.status == FileChangeStatus.REMOVED:
        return _unmappable(UnmappableReason.FILE_DELETED, f"{finding_path!r} was deleted in this PR")

    if changed_file.patch is None or not diff_file.hunks:
        return _unmappable(UnmappableReason.BINARY_OR_NO_PATCH, f"{finding_path!r} has no diffable patch (binary or too large)")

    new_line_map: dict[int, int] = {}  # new_line_number -> hunk index
    for hunk_index, hunk in enumerate(diff_file.hunks):
        for line in hunk.lines:
            if line.line_type is DiffLineType.DELETION:
                continue
            if line.new_line_number is not None:
                new_line_map[line.new_line_number] = hunk_index

    if start_line == end_line:
        if start_line in new_line_map:
            return _mappable(MappedPosition(path=finding_path, side=DiffSide.NEW, line=start_line))
        return _unmappable(
            UnmappableReason.LINE_NOT_IN_DIFF,
            f"line {start_line} of {finding_path!r} is not part of any diff hunk",
        )

    lo, hi = min(start_line, end_line), max(start_line, end_line)
    range_lines = range(lo, hi + 1)
    hunk_indexes = {new_line_map.get(n) for n in range_lines}
    if len(hunk_indexes) == 1 and None not in hunk_indexes:
        return _mappable(
            MappedPosition(path=finding_path, side=DiffSide.NEW, line=hi, start_side=DiffSide.NEW, start_line=lo)
        )

    # Ambiguous/partial multi-line range (section 11: never guess) --
    # fall back to a single valid line, preferring the range's end (the
    # line closest to what the finding is actually about) then its start.
    if hi in new_line_map:
        return _mappable(MappedPosition(path=finding_path, side=DiffSide.NEW, line=hi))
    if lo in new_line_map:
        return _mappable(MappedPosition(path=finding_path, side=DiffSide.NEW, line=lo))

    return _unmappable(
        UnmappableReason.LINE_NOT_IN_DIFF,
        f"no line in range {lo}-{hi} of {finding_path!r} is part of any diff hunk",
    )
