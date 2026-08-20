"""Git-based change classification between two indexed commits.

This only classifies *which paths changed* for observability and the
``incremental`` decision — it does not itself decide what gets
reparsed. Reuse is driven by content hashes via the parse cache (see
:mod:`patchfrog.indexing.parse_cache` and
:mod:`patchfrog.indexing.service`), which handles renames and
no-op changes correctly without consulting this module at all. This
module exists to answer "what changed and how" for logging, metrics, and
the added/modified/deleted/renamed classification the indexing summary
reports.
"""

from __future__ import annotations

from patchfrog.indexing.models import ChangeSet, FileChange, FileChangeType
from patchfrog.repository.git import run_git
from patchfrog.repository.snapshot import RepositorySnapshot

_RENAME_SIMILARITY_THRESHOLD = "50%"


def compute_change_set(
    snapshot: RepositorySnapshot, *, old_commit_sha: str | None
) -> ChangeSet:
    """Classify tracked-file changes between ``old_commit_sha`` and the snapshot's commit.

    ``old_commit_sha`` must already be a locally reachable commit in
    ``snapshot`` (see :meth:`RepositorySnapshotProvider.acquire`'s
    ``also_fetch`` parameter) — this only runs ``git diff``, it never
    fetches. A ``None`` old SHA means "no previous index" and is reported
    as a full reindex with no individual changes classified.
    """

    if old_commit_sha is None:
        return ChangeSet(old_commit_sha=None, new_commit_sha=snapshot.commit_sha, changes=())

    output = run_git(
        [
            "-C",
            str(snapshot.root_path),
            "diff",
            "--name-status",
            f"-M{_RENAME_SIMILARITY_THRESHOLD}",
            "-z",
            old_commit_sha,
            snapshot.commit_sha,
        ]
    )
    return parse_name_status_diff(output, old_commit_sha=old_commit_sha, new_commit_sha=snapshot.commit_sha)


def parse_name_status_diff(output: str, *, old_commit_sha: str | None, new_commit_sha: str) -> ChangeSet:
    """Public parsing half of :func:`compute_change_set`, split out so
    :mod:`patchfrog.repository.ancestry` (Phase 7) can reuse the exact
    same ``git diff --name-status -M50% -z`` parsing against a scratch
    clone it fetched for a different reason (ancestry proof), without a
    second fetch or a :class:`RepositorySnapshot` of its own."""

    return ChangeSet(
        old_commit_sha=old_commit_sha, new_commit_sha=new_commit_sha, changes=tuple(_parse_name_status(output))
    )


def _parse_name_status(output: str) -> list[FileChange]:
    fields = output.split("\0")
    changes: list[FileChange] = []
    i = 0
    while i < len(fields):
        record = fields[i]
        i += 1
        if not record:
            continue

        status = record[0]
        if status == "A":
            changes.append(FileChange(change_type=FileChangeType.ADDED, path=fields[i]))
            i += 1
        elif status == "M":
            changes.append(FileChange(change_type=FileChangeType.MODIFIED, path=fields[i]))
            i += 1
        elif status == "D":
            changes.append(FileChange(change_type=FileChangeType.DELETED, path=fields[i]))
            i += 1
        elif status == "R":
            old_path, new_path = fields[i], fields[i + 1]
            i += 2
            changes.append(
                FileChange(
                    change_type=FileChangeType.RENAMED, path=new_path, previous_path=old_path
                )
            )
        # Other statuses (C copy, T type-change, U unmerged, X unknown) are
        # not classified here — a copy/type-change still shows up as a plain
        # add/modify of its destination path via the working-tree inventory,
        # so nothing is silently lost, just not specially labeled.
        else:
            i += 1

    return changes
