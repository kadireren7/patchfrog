"""Credential-free artifact staging for the verifier hand-off --
Milestone S6, corrected.

**Security correction, not the original design.** The first version of
this module staged the *entire git checkout* -- `.git/` included -- into
the verifier-visible shared volume. `RepositorySnapshotProvider` injects
the GitHub installation token directly into the git remote URL
(``git remote add origin <credential-bearing URL>``), which git stores in
plaintext in ``.git/config``. A real, synthetic-token reproduction proved
a sandboxed pytest test could read that file and recover the token --
see ``validation/production_execution/latest-summary.md`` section 11 for
the full empirical transcript. Network denial does not mitigate this: a
credential read from disk and printed to bounded stdout still crosses
back into the trusted review worker as "evidence."

**The fix**: the review worker never hands the verifier anything with
`.git/` in it at all. `git archive` -- a real, already-battle-tested git
primitive with no concept of remotes or credentials -- exports *only*
the tracked, committed content at the exact commit, directly from the
object database, into a fresh directory. There is nothing to leak by
construction, not merely by careful exclusion. The credential-bearing
clone this function still needs internally (to actually reach the
commit) lives in an isolated, never-shared temp directory and is deleted
immediately after the archive is extracted -- its lifetime ends before
any verifier-visible request is even built.

**Integrity**: a deterministic content-manifest digest
(:func:`compute_artifact_digest`) over the exported artifact's actual
bytes -- relative path, entry kind, and content (files) or raw link
target (symlinks, never dereferenced) -- not git metadata. The verifier
recomputes the identical digest over its own disposable copy of the
artifact (see
:func:`patchfrog.executable_verification.service.execute_against_snapshot`)
immediately before running anything, which is also this milestone's
TOCTOU fix: the content that gets checked is the exact, already-private
content that gets executed, because they are the same bytes.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from patchfrog.repository.git import GitError
from patchfrog.repository.snapshot import RepositorySnapshotProvider

_ARCHIVE_TIMEOUT_SECONDS = 60.0


class ArtifactExportError(RuntimeError):
    """The credential-free artifact could not be produced -- the caller
    must treat this exactly like any other staging failure (verification
    simply unavailable for this run), never as a reason to fall back to
    handing over the credential-bearing checkout instead."""


def export_artifact(
    *,
    clone_url: str,
    commit_sha: str,
    repository_full_name: str,
    token: str | None,
    destination_root: Path,
) -> Path:
    """Acquires ``commit_sha`` using a real, isolated, credential-bearing
    clone (never shared, never returned, deleted before this function
    returns), then exports *only* its tracked content at that exact
    commit -- via ``git archive``, which has no concept of ``.git/``,
    remotes, or credentials -- into a fresh directory under
    ``destination_root``. Returns the path to that credential-free
    artifact directory.

    Raises :class:`ArtifactExportError` on any failure (network error,
    bad token, git archive failure) -- the caller must treat this as
    "verification unavailable this run," never retry with a weaker
    mechanism.
    """

    provider = RepositorySnapshotProvider()
    try:
        snapshot = provider.acquire(
            clone_url=clone_url, commit_sha=commit_sha, repository_full_name=repository_full_name, token=token,
        )
    except GitError as exc:
        raise ArtifactExportError(f"could not acquire repository: {exc}") from exc

    try:
        artifact_dir = Path(tempfile.mkdtemp(prefix="patchfrog-artifact-", dir=destination_root))
        try:
            archive = subprocess.run(
                ["git", "-c", "core.hooksPath=/dev/null", "archive", commit_sha],
                cwd=snapshot.root_path, capture_output=True, timeout=_ARCHIVE_TIMEOUT_SECONDS, check=False,
            )
            if archive.returncode != 0:
                raise ArtifactExportError(
                    f"git archive failed (exit {archive.returncode}): {archive.stderr.decode('utf-8', errors='replace')}"
                )
            extract = subprocess.run(
                ["tar", "-x", "-C", str(artifact_dir)],
                input=archive.stdout, capture_output=True, timeout=_ARCHIVE_TIMEOUT_SECONDS, check=False,
            )
            if extract.returncode != 0:
                raise ArtifactExportError(
                    f"artifact extraction failed (exit {extract.returncode}): "
                    f"{extract.stderr.decode('utf-8', errors='replace')}"
                )
        except (OSError, subprocess.TimeoutExpired) as exc:
            shutil.rmtree(artifact_dir, ignore_errors=True)
            raise ArtifactExportError(f"artifact export failed: {exc}") from exc
    finally:
        # The credential-bearing clone's lifetime ends here, unconditionally
        # -- before this function returns, before any request is built.
        snapshot.cleanup()

    return artifact_dir


def compute_artifact_digest(root_path: Path) -> str:
    """A deterministic content-manifest digest over ``root_path``'s
    actual on-disk bytes -- never git metadata, never host-absolute
    paths, stable ordering. Each entry is one of:

    - ``file:<relative path>:<sha256 of content>``
    - ``symlink:<relative path>:<raw link target string>`` (the target is
      recorded, never followed/dereferenced -- this only works correctly
      when the caller copied the tree with symlinks preserved, e.g.
      ``shutil.copytree(..., symlinks=True)``, never the default
      ``symlinks=False``, which would dereference and hash the *target's*
      content under the symlink's own name)
    - ``dir:<relative path>`` (only for an otherwise-empty directory, so
      an injected empty directory is still represented)
    """

    entries: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root_path, followlinks=False):
        # A symlinked directory appears in dirnames, not filenames, and
        # os.walk never descends into it (followlinks=False) -- but it
        # must still be represented, or a malicious directory-symlink
        # would be silently invisible to the digest while still being
        # faithfully preserved (and executable) by shutil.copytree(...,
        # symlinks=True). Recorded here, then excluded from further
        # traversal explicitly (os.walk already wouldn't descend, but
        # this keeps the intent unambiguous).
        real_subdirs = []
        for name in dirnames:
            full = Path(dirpath) / name
            if full.is_symlink():
                rel = full.relative_to(root_path).as_posix()
                entries.append(f"symlink:{rel}:{os.readlink(full)}")
            else:
                real_subdirs.append(name)
        dirnames[:] = real_subdirs

        dir_rel = Path(dirpath).relative_to(root_path)
        if not dirnames and not filenames and dir_rel != Path("."):
            entries.append(f"dir:{dir_rel.as_posix()}")

        for name in filenames:
            full = Path(dirpath) / name
            rel = full.relative_to(root_path).as_posix()
            if full.is_symlink():
                entries.append(f"symlink:{rel}:{os.readlink(full)}")
            else:
                content_hash = hashlib.sha256(full.read_bytes()).hexdigest()
                entries.append(f"file:{rel}:{content_hash}")

    entries.sort()
    hasher = hashlib.sha256()
    for entry in entries:
        hasher.update(entry.encode("utf-8"))
        hasher.update(b"\n")
    return hasher.hexdigest()
