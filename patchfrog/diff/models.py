"""Normalized unified-diff representation.

This is the model future review logic will query ("which new lines were
introduced", "which GitHub line can receive an inline comment", "which
function contains line 84"). Phase 1 only needs to answer the first
question, but the shape is deliberately generic enough not to block the
others.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class DiffLineType(StrEnum):
    """The kind of a single line within a diff hunk."""

    CONTEXT = "context"
    ADDITION = "addition"
    DELETION = "deletion"


@dataclass(frozen=True, slots=True)
class DiffLine:
    """A single line within a diff hunk.

    Exactly one of ``old_line_number`` / ``new_line_number`` is ``None`` for
    additions/deletions; both are set for context lines.
    """

    line_type: DiffLineType
    old_line_number: int | None
    new_line_number: int | None
    content: str


@dataclass(frozen=True, slots=True)
class DiffHunk:
    """A contiguous block of changes, as delimited by an ``@@`` header."""

    old_start: int
    old_lines: int
    new_start: int
    new_lines: int
    section_heading: str | None
    lines: tuple[DiffLine, ...] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class DiffFile:
    """The normalized diff for a single file within a pull request.

    ``hunks`` is empty when GitHub provided no patch for the file (e.g. a
    binary file, or a file too large to diff).
    """

    path: str
    hunks: tuple[DiffHunk, ...] = field(default_factory=tuple)

    @property
    def added_lines(self) -> list[DiffLine]:
        """All lines introduced by this file's diff, in file order."""

        return [
            line
            for hunk in self.hunks
            for line in hunk.lines
            if line.line_type is DiffLineType.ADDITION
        ]

    @property
    def deleted_lines(self) -> list[DiffLine]:
        """All lines removed by this file's diff, in file order."""

        return [
            line
            for hunk in self.hunks
            for line in hunk.lines
            if line.line_type is DiffLineType.DELETION
        ]
