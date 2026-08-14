"""Domain-facing import path for the normalized diff representation.

The concrete types live in :mod:`patchfrog.diff.models`, alongside the
parser that produces them. This module re-exports them so the rest of the
domain layer (and its consumers) can depend on ``patchfrog.domain.diff``
without reaching into the parsing subpackage directly.
"""

from __future__ import annotations

from patchfrog.diff.models import DiffFile, DiffHunk, DiffLine, DiffLineType

__all__ = ["DiffFile", "DiffHunk", "DiffLine", "DiffLineType"]
