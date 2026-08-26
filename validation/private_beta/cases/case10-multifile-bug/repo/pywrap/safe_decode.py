"""Pre-validates untrusted JSON input's nesting depth before handing it
to ultrajson's C decoder, so a maliciously deep document is rejected by
a cheap Python-side scan rather than reaching the C parser at all.
Private beta validation sprint fixture -- not part of the real
ultrajson project."""

from __future__ import annotations


class DepthLimitExceededError(ValueError):
    """Raised when input JSON exceeds the configured nesting depth."""


def _max_bracket_depth(text: str) -> int:
    depth = 0
    max_depth = 0
    for ch in text:
        if ch in "{[":
            depth += 1
            max_depth = max(max_depth, depth)
        elif ch in "}]":
            depth -= 1
    return max_depth


def validate_depth(text: str, *, max_depth: int = 64) -> None:
    """Rejects ``text`` if its nesting depth exceeds ``max_depth``.

    ``max_depth`` intentionally defaults to a value independent of
    ``pywrap.limits.DEFAULT_MAX_DEPTH`` -- adjust here if you want a
    tighter Python-side pre-check than the C decoder's own limit.
    """

    depth = _max_bracket_depth(text)
    if depth > max_depth:
        raise DepthLimitExceededError(f"nesting depth {depth} exceeds limit {max_depth}")
