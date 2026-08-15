"""Changed-line / changed-symbol classification.

Given Phase 1's parsed PR diff and a finding's location, determines
whether the finding sits directly on a changed (added) line, and/or
inside a symbol that changed even if the finding's own line didn't. Both
are persisted — never used to drop non-changed findings here; filtering
for review presentation is a later phase's concern.
"""

from __future__ import annotations

from dataclasses import dataclass

from patchfrog.diff.models import DiffFile
from patchfrog.domain.code import SourceSpan


@dataclass(frozen=True, slots=True)
class ChangedLineClassification:
    is_on_changed_line: bool
    is_in_changed_symbol: bool


def build_changed_lines_by_file(diff_files: list[DiffFile]) -> dict[str, frozenset[int]]:
    """The set of new-file line numbers a PR actually added, per file."""

    return {
        diff_file.path: frozenset(
            line.new_line_number for line in diff_file.added_lines if line.new_line_number is not None
        )
        for diff_file in diff_files
    }


def classify(
    *,
    file_path: str,
    span: SourceSpan,
    changed_lines_by_file: dict[str, frozenset[int]],
    containing_symbol_span: SourceSpan | None,
) -> ChangedLineClassification:
    changed_lines = changed_lines_by_file.get(file_path, frozenset())

    is_on_changed_line = any(
        line in changed_lines for line in range(span.start_line, span.end_line + 1)
    )

    is_in_changed_symbol = False
    if containing_symbol_span is not None:
        is_in_changed_symbol = any(
            line in changed_lines
            for line in range(containing_symbol_span.start_line, containing_symbol_span.end_line + 1)
        )

    return ChangedLineClassification(
        is_on_changed_line=is_on_changed_line, is_in_changed_symbol=is_in_changed_symbol
    )
