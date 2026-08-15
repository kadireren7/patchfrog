"""Repository snapshot acquisition.

Acquires a local, on-disk checkout of a repository at a specific commit
SHA for indexing. Repository contents are untrusted input: nothing here
ever executes code from the checked-out tree (no ``make``, no package
installs, no git hooks — see :mod:`patchfrog.repository.git`).
"""

from __future__ import annotations

import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from urllib.parse import urlsplit, urlunsplit

from patchfrog.repository.git import GitError, run_git

_SHALLOW_FETCH_TIMEOUT_SECONDS = 120.0


@dataclass(frozen=True, slots=True)
class RepositorySnapshot:
    """An on-disk checkout of one repository at one commit.

    Use as a context manager to guarantee cleanup of the checkout
    directory::

        with provider.acquire(...) as snapshot:
            ...

    ``owns_root`` is ``False`` for a snapshot built from a pre-existing
    local checkout (see :meth:`RepositorySnapshotProvider.acquire_local`)
    — cleanup must never delete a directory PatchFrog didn't create.
    """

    repository_full_name: str
    commit_sha: str
    root_path: Path
    owns_root: bool = True

    def __enter__(self) -> RepositorySnapshot:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.cleanup()

    def cleanup(self) -> None:
        if self.owns_root:
            shutil.rmtree(self.root_path, ignore_errors=True)

    def resolve_path(self, relative_path: str) -> Path:
        """Resolve a repo-relative path, rejecting traversal outside the root.

        Guards against a malicious or mistaken relative path (e.g. from a
        symlink or crafted file inventory entry) escaping the checkout.
        """

        root = self.root_path.resolve()
        candidate = (root / relative_path).resolve()
        if candidate != root and root not in candidate.parents:
            raise ValueError(f"Path escapes repository root: {relative_path!r}")
        return candidate


class RepositorySnapshotProvider:
    """Acquires :class:`RepositorySnapshot` instances via a targeted git fetch."""

    def __init__(self, *, workdir_root: Path | None = None) -> None:
        self._workdir_root = workdir_root

    def acquire(
        self,
        *,
        clone_url: str,
        commit_sha: str,
        repository_full_name: str,
        token: str | None = None,
        also_fetch: list[str] | None = None,
    ) -> RepositorySnapshot:
        """Check out ``commit_sha`` from ``clone_url`` into a fresh temp directory.

        ``also_fetch`` additionally fetches (but does not check out) more
        commits into the same local repository — used for incremental
        indexing, where :func:`patchfrog.indexing.incremental.compute_change_set`
        needs the previous index's commit object available locally to diff
        against.
        """

        root_path = Path(
            tempfile.mkdtemp(prefix="patchfrog-snapshot-", dir=self._workdir_root)
        )
        try:
            self._fetch_commit(
                clone_url=clone_url,
                commit_sha=commit_sha,
                token=token,
                dest=root_path,
                also_fetch=also_fetch or [],
            )
        except GitError:
            shutil.rmtree(root_path, ignore_errors=True)
            raise

        return RepositorySnapshot(
            repository_full_name=repository_full_name,
            commit_sha=commit_sha,
            root_path=root_path,
        )

    def acquire_local(
        self, *, root_path: Path, repository_full_name: str
    ) -> RepositorySnapshot:
        """Wrap an existing local git checkout as a snapshot, without copying it.

        For developer/CLI use against a repository already on disk (e.g.
        self-validating PatchFrog against its own checkout). The returned
        snapshot never deletes ``root_path`` on cleanup.
        """

        commit_sha = run_git(["-C", str(root_path), "rev-parse", "HEAD"]).strip()
        return RepositorySnapshot(
            repository_full_name=repository_full_name,
            commit_sha=commit_sha,
            root_path=root_path,
            owns_root=False,
        )

    def _fetch_commit(
        self,
        *,
        clone_url: str,
        commit_sha: str,
        token: str | None,
        dest: Path,
        also_fetch: list[str],
    ) -> None:
        url = _inject_token(clone_url, token) if token else clone_url

        run_git(["init", "--quiet", str(dest)], timeout_seconds=_SHALLOW_FETCH_TIMEOUT_SECONDS)
        run_git(
            ["-C", str(dest), "remote", "add", "origin", url],
            timeout_seconds=_SHALLOW_FETCH_TIMEOUT_SECONDS,
        )
        for sha in [commit_sha, *also_fetch]:
            self._fetch_one(dest=dest, sha=sha)
        run_git(
            ["-C", str(dest), "checkout", "--quiet", "--detach", commit_sha],
            timeout_seconds=_SHALLOW_FETCH_TIMEOUT_SECONDS,
        )
        # Submodules are never initialized: doing so would fetch and
        # potentially execute config from arbitrary, untrusted URLs.

    def _fetch_one(self, *, dest: Path, sha: str) -> None:
        try:
            run_git(
                ["-C", str(dest), "fetch", "--quiet", "--depth", "1", "origin", sha],
                timeout_seconds=_SHALLOW_FETCH_TIMEOUT_SECONDS,
            )
        except GitError:
            # Not every server allows fetching an arbitrary SHA directly
            # (only reachable-SHA1-in-want servers do). Fall back to
            # fetching everything and resolving the SHA locally.
            run_git(
                ["-C", str(dest), "fetch", "--quiet", "origin"],
                timeout_seconds=_SHALLOW_FETCH_TIMEOUT_SECONDS,
            )


def _inject_token(clone_url: str, token: str) -> str:
    parsed = urlsplit(clone_url)
    netloc = f"x-access-token:{token}@{parsed.hostname}"
    if parsed.port:
        netloc += f":{parsed.port}"
    return urlunsplit(parsed._replace(netloc=netloc))
