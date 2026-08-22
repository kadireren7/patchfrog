"""Internal domain models for pull requests and their changed files.

These types are what the rest of PatchFrog operates on after data has been
fetched from the GitHub API and translated at the GitHub boundary
(:mod:`patchfrog.github.client`). The raw GitHub JSON schema never leaks
past that boundary.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


@dataclass(frozen=True, slots=True)
class PullRequestRef:
    """Minimal reference to address a specific pull request via the GitHub API."""

    owner: str
    repository: str
    number: int


class FileChangeStatus(StrEnum):
    """Status of a file within a pull request diff."""

    ADDED = "added"
    MODIFIED = "modified"
    REMOVED = "removed"
    RENAMED = "renamed"
    COPIED = "copied"
    CHANGED = "changed"
    UNCHANGED = "unchanged"


@dataclass(frozen=True, slots=True)
class PullRequestMetadata:
    """Normalized pull request metadata, as retrieved from the GitHub API."""

    number: int
    title: str
    body: str | None
    author: str
    base_branch: str
    head_branch: str
    base_sha: str
    head_sha: str
    html_url: str
    state: str
    #: GitHub's own ``merged`` boolean -- distinct from ``state`` (a
    #: merged PR always has ``state == "closed"``, but a closed PR is not
    #: necessarily merged). See :mod:`patchfrog.feedback.sync` for
    #: ``PR_MERGED`` vs. ``PR_CLOSED`` lifecycle event derivation.
    merged: bool = False


@dataclass(frozen=True, slots=True)
class ChangedFile:
    """A single file changed within a pull request."""

    path: str
    previous_path: str | None
    status: FileChangeStatus
    additions: int
    deletions: int
    patch: str | None
