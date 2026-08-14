"""Parser for the unified-diff ``patch`` text GitHub returns per file.

GitHub's "list pull request files" API returns, for each changed file, a
``patch`` string containing one or more unified-diff hunks (no
``diff --git`` / ``---``/``+++`` file headers — those are implied by the
surrounding file object). This module parses that hunk text into the
normalized :mod:`patchfrog.diff.models` representation.
"""

from __future__ import annotations

import re

from patchfrog.diff.models import DiffFile, DiffHunk, DiffLine, DiffLineType

_HUNK_HEADER_RE = re.compile(
    r"^@@ -(?P<old_start>\d+)(?:,(?P<old_lines>\d+))? "
    r"\+(?P<new_start>\d+)(?:,(?P<new_lines>\d+))? @@(?P<heading>.*)$"
)

_NO_NEWLINE_MARKER = "\\ No newline at end of file"


class PatchParseError(ValueError):
    """Raised when a patch does not conform to the expected unified-diff shape."""


def parse_patch(patch: str | None) -> tuple[DiffHunk, ...]:
    """Parse a GitHub per-file unified-diff patch into normalized hunks.

    Returns an empty tuple for ``None`` or empty patches (binary files,
    files GitHub declined to diff, or genuinely empty changes).
    """

    if not patch:
        return ()

    lines = patch.split("\n")
    hunks: list[DiffHunk] = []
    current_lines: list[DiffLine] = []
    hunk_meta: tuple[int, int, int, int, str | None] | None = None
    old_cursor = 0
    new_cursor = 0

    def flush() -> None:
        nonlocal hunk_meta
        if hunk_meta is None:
            return
        old_start, old_lines, new_start, new_lines, heading = hunk_meta
        hunks.append(
            DiffHunk(
                old_start=old_start,
                old_lines=old_lines,
                new_start=new_start,
                new_lines=new_lines,
                section_heading=heading,
                lines=tuple(current_lines),
            )
        )
        current_lines.clear()
        hunk_meta = None

    for raw_line in lines:
        if raw_line.startswith("@@ "):
            match = _HUNK_HEADER_RE.match(raw_line)
            if match is None:
                raise PatchParseError(f"Malformed hunk header: {raw_line!r}")
            flush()
            old_start = int(match.group("old_start"))
            new_start = int(match.group("new_start"))
            old_lines = int(match.group("old_lines") or "1")
            new_lines = int(match.group("new_lines") or "1")
            heading = match.group("heading").strip() or None
            hunk_meta = (old_start, old_lines, new_start, new_lines, heading)
            old_cursor = old_start
            new_cursor = new_start
            continue

        if hunk_meta is None:
            if raw_line == "":
                # Trailing newline artifact from `patch.split("\n")`.
                continue
            raise PatchParseError(f"Content line outside of any hunk: {raw_line!r}")

        if raw_line.startswith(_NO_NEWLINE_MARKER):
            # Annotation, not a content line — the preceding line simply
            # lacks a trailing newline in the original file.
            continue

        if raw_line == "":
            # A genuinely blank line missing its context marker only shows
            # up as the harmless split() artifact at end-of-patch, handled
            # above via `hunk_meta is None`. Inside a hunk this can only be
            # a blank context line whose marker was an empty string split
            # artifact of a trailing "\n"; ignore rather than fail.
            continue

        marker, content = raw_line[0], raw_line[1:]
        if marker == " ":
            current_lines.append(
                DiffLine(
                    line_type=DiffLineType.CONTEXT,
                    old_line_number=old_cursor,
                    new_line_number=new_cursor,
                    content=content,
                )
            )
            old_cursor += 1
            new_cursor += 1
        elif marker == "-":
            current_lines.append(
                DiffLine(
                    line_type=DiffLineType.DELETION,
                    old_line_number=old_cursor,
                    new_line_number=None,
                    content=content,
                )
            )
            old_cursor += 1
        elif marker == "+":
            current_lines.append(
                DiffLine(
                    line_type=DiffLineType.ADDITION,
                    old_line_number=None,
                    new_line_number=new_cursor,
                    content=content,
                )
            )
            new_cursor += 1
        else:
            raise PatchParseError(f"Unrecognized diff line marker in: {raw_line!r}")

    flush()
    return tuple(hunks)


def build_diff_file(path: str, patch: str | None) -> DiffFile:
    """Build a :class:`DiffFile` for a single changed file from its patch text."""

    return DiffFile(path=path, hunks=parse_patch(patch))
