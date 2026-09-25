"""Endpoint path replacement inside string literals (Python and JS/TS).

``/repos/{owner}/{repo}/legacy-stats`` -> ``/repos/{owner}/{repo}/stats/summary``
is applied to the literal *source text* of the usage-site line: literal
path segments must match exactly; each template placeholder segment
matches whatever the code has there (``{owner}``, ``${owner}``, a
concrete value) and is carried over to the new path by position. Nothing
outside the matched path text changes (host, query string, quotes).
"""

from __future__ import annotations

import re

_PLACEHOLDER = r"(\$\{[^}]*\}|\{[^}]*\}|[^/\"'`\s?#{}]+)"


def _segments(path: str) -> list[str]:
    return [s for s in path.strip("/").split("/") if s]


def path_regex(path: str) -> re.Pattern[str]:
    parts = [_PLACEHOLDER if s.startswith("{") and s.endswith("}") else re.escape(s) for s in _segments(path)]
    return re.compile("/" + "/".join(parts) + r"(?=[\"'`?#]|$)")


def replace_path(text: str, old_path: str, new_path: str) -> list[tuple[int, int, str]]:
    """(start, end, replacement) for every occurrence of ``old_path`` in
    ``text``; placeholders are mapped to the new path by position."""

    new_segments = _segments(new_path)
    edits: list[tuple[int, int, str]] = []
    for match in path_regex(old_path).finditer(text):
        values = iter(match.groups())
        rebuilt = []
        for segment in new_segments:
            if segment.startswith("{") and segment.endswith("}"):
                rebuilt.append(next(values))
            else:
                rebuilt.append(segment)
        edits.append((match.start(), match.end(), "/" + "/".join(rebuilt)))
    return edits


def contains_path(text: str, path: str) -> bool:
    return path_regex(path).search(text) is not None


__all__ = ["contains_path", "path_regex", "replace_path"]
