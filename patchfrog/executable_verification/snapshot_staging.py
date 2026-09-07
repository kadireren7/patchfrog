"""Repository snapshot staging for the verifier hand-off -- Milestone S6.

The review worker (which holds the GitHub installation token) materializes
one exact-head checkout **once per review run** into a location the
verifier process can also read (a shared volume in a containerized
deployment, or simply a shared filesystem path for a bare-host verifier),
and hands the verifier a path -- never a GitHub credential. Reuses
:class:`patchfrog.repository.snapshot.RepositorySnapshotProvider` directly;
this module adds only the shared-location + integrity concern, never a
second clone mechanism.

**Integrity, not signing** (Part H): git's own native identity/cleanliness
checks, not an invented hashing scheme. ``git rev-parse HEAD`` binds
identity (this really is a checkout of the expected commit); ``git
diff-index --quiet HEAD --`` binds content (the working tree matches that
commit's tree exactly, byte for byte -- git compares real on-disk file
content, not cached metadata). An earlier draft of this module used
``git rev-parse HEAD^{tree}`` as a "digest" and was found, empirically, to
be a **false** integrity signal: that command reads the *committed* tree
object from ``.git``'s own object database and is completely blind to
tampering with the actual on-disk working-tree files (verified: editing a
tracked file in place left ``HEAD^{tree}`` completely unchanged). Both
checks below inspect real, current on-disk content.
"""

from __future__ import annotations

from pathlib import Path

from patchfrog.repository.git import GitError, run_git
from patchfrog.repository.snapshot import RepositorySnapshot, RepositorySnapshotProvider

_INTEGRITY_CHECK_TIMEOUT_SECONDS = 30.0


def stage_snapshot(
    *,
    clone_url: str,
    commit_sha: str,
    repository_full_name: str,
    token: str | None,
    shared_root: Path | None,
) -> RepositorySnapshot:
    """Materialize an exact-head checkout under ``shared_root`` (a
    verifier-visible shared location). The caller owns the returned
    snapshot's lifecycle (cleanup once every verification attempt for
    this review run is done) -- this function never deletes anything
    itself."""

    provider = RepositorySnapshotProvider(workdir_root=shared_root)
    return provider.acquire(
        clone_url=clone_url, commit_sha=commit_sha, repository_full_name=repository_full_name, token=token,
    )


def verify_snapshot_integrity(*, root_path: Path, expected_commit_sha: str) -> bool:
    """Returns ``True`` only if ``root_path`` is a real git checkout whose
    ``HEAD`` is exactly ``expected_commit_sha`` *and* whose working tree
    has zero content difference from that commit -- both a real,
    on-disk-content-based check, never merely comparing cached metadata.
    Never raises: any git failure (not a checkout, corrupted, path
    missing) is itself a "cannot trust this snapshot" signal, identical
    to a genuine mismatch -- the caller must fail closed either way."""

    try:
        actual_head = run_git(
            ["-C", str(root_path), "rev-parse", "HEAD"], timeout_seconds=_INTEGRITY_CHECK_TIMEOUT_SECONDS,
        ).strip()
        if actual_head != expected_commit_sha:
            return False
        run_git(
            ["-C", str(root_path), "diff-index", "--quiet", "HEAD", "--"],
            timeout_seconds=_INTEGRITY_CHECK_TIMEOUT_SECONDS,
        )
    except GitError:
        return False
    return True
