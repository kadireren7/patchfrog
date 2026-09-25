"""Text edits: the one transformation primitive of the patch generator.

Every rewriter (Python, JS/TS, manifests) turns a planned
:class:`~patchfrog.migration.domain.EditOperation` into span edits against
the *original* file text. Everything outside the spans is left
byte-for-byte untouched, so formatting, comments and unrelated code are
preserved. Replacements never contain a newline, which keeps line
numbers stable -- the safety gates rely on that to re-check each usage
site at its original line.
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass
from itertools import pairwise


class RewriteError(Exception):
    """The edit cannot be made safely at this site (step FAILED)."""


class NotNeeded(Exception):
    """The code already has the target shape, or does not use the
    changed element (step NOT_NEEDED)."""


@dataclass(frozen=True, slots=True)
class TextEdit:
    file_path: str
    start: int
    end: int
    replacement: str
    step_id: str
    #: Inclusive 1-based line range this edit is allowed to touch (the
    #: usage-site call, the calling function, or the manifest line).
    scope: tuple[int, int]
    #: ``primary`` (at the usage site) | ``secondary`` (e.g. a reference
    #: to a moved module, a read of a renamed result field).
    role: str = "primary"


def line_of(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def apply_edits(text: str, edits: list[TextEdit]) -> str:
    ordered = sorted(edits, key=lambda e: (e.start, e.end))
    for first, second in pairwise(ordered):
        if second.start < first.end:
            raise RewriteError("overlapping edits")
    out = text
    for edit in reversed(ordered):
        out = out[: edit.start] + edit.replacement + out[edit.end :]
    return out


def overlaps(edit: TextEdit, others: list[TextEdit]) -> bool:
    return any(edit.start < other.end and other.start < edit.end for other in others)


def unified_diff(path: str, before: str, after: str) -> str:
    """A ``git apply``-compatible unified diff for one file."""

    if before == after:
        return ""
    lines = list(
        difflib.unified_diff(
            before.splitlines(keepends=True), after.splitlines(keepends=True),
            fromfile=f"a/{path}", tofile=f"b/{path}", n=3,
        )
    )
    out: list[str] = []
    for line in lines:
        if line.endswith("\n"):
            out.append(line)
        else:
            out.append(line + "\n\\ No newline at end of file\n")
    return "".join(out)


__all__ = ["NotNeeded", "RewriteError", "TextEdit", "apply_edits", "line_of", "overlaps", "unified_diff"]
