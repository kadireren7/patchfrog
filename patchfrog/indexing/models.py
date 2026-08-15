"""Domain models for the indexing *process* itself (not parsed code —
see :mod:`patchfrog.domain.code` for that). These describe file
inventory entries, change classification for incremental indexing, and
the summary/metrics of a completed indexing run.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from patchfrog.domain.code import Language


@dataclass(frozen=True, slots=True)
class FileInventoryEntry:
    """One tracked, indexable file discovered in a repository snapshot."""

    relative_path: str
    language: Language | None
    size_bytes: int
    content_hash: str
    git_blob_sha: str | None
    is_test: bool
    is_generated: bool


class FileChangeType(StrEnum):
    """How a file differs between two indexed commits."""

    ADDED = "added"
    MODIFIED = "modified"
    DELETED = "deleted"
    RENAMED = "renamed"


@dataclass(frozen=True, slots=True)
class FileChange:
    change_type: FileChangeType
    path: str
    previous_path: str | None = None


@dataclass(frozen=True, slots=True)
class ChangeSet:
    """The set of tracked-file changes between two commits, from git."""

    old_commit_sha: str | None
    new_commit_sha: str
    changes: tuple[FileChange, ...] = field(default_factory=tuple)

    @property
    def is_full_reindex(self) -> bool:
        return self.old_commit_sha is None


@dataclass(frozen=True, slots=True)
class IndexingSummary:
    """Metrics for one completed (or failed) indexing run."""

    files_total: int
    files_parsed: int
    files_failed: int
    files_reused: int
    symbols_extracted: int
    edges_created: int
    duration_ms: float
    incremental: bool
