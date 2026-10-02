"""Typed GitHub Git Data API transport models -- the primitives M9 needs
to build a migration branch/commit without ever shelling out to a local
``git`` binary (no clone, no credential-bearing remote URL, no local
working tree at all: every write is a plain, typed GitHub API call)."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class GitTreeEntry:
    """One file's desired content in a new commit tree. ``content`` is
    the file's full new text content -- GitHub's Git Data API creates the
    backing blob server-side, so no separate blob-creation round trip is
    needed for text files."""

    path: str
    content: str
    mode: str = "100644"


__all__ = ["GitTreeEntry"]
