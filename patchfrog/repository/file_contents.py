"""Deterministic, exact-current-commit file content reads for Phase 7
evidence revalidation (:mod:`patchfrog.review_memory.evidence`).

A lightweight, single-commit fetch (``git show <sha>:<path>`` needs no
working-tree checkout) -- deliberately separate from and cheaper than
:mod:`patchfrog.repository.snapshot`'s full checkout, since evidence
revalidation only ever needs specific file blobs at one exact commit,
never the whole tree on disk. Never executes anything from the fetched
content -- this only ever runs ``git fetch``/``git show`` (both disable
hooks; see :mod:`patchfrog.repository.git`).
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from patchfrog.repository.git import GitError, run_git

_FETCH_TIMEOUT_SECONDS = 120.0


def _inject_token(clone_url: str, token: str) -> str:
    parsed = urlsplit(clone_url)
    netloc = f"x-access-token:{token}@{parsed.hostname}"
    if parsed.port:
        netloc += f":{parsed.port}"
    return urlunsplit(parsed._replace(netloc=netloc))


def read_files_at_commit(
    *,
    clone_url: str,
    commit_sha: str,
    paths: frozenset[str],
    token: str | None = None,
    workdir_root: Path | None = None,
) -> dict[str, str | None]:
    """Fetch ``commit_sha`` (shallow, single commit) and read each of
    ``paths``'s exact blob content at that commit via ``git show``.

    A path maps to ``None`` (never an empty string, never silently
    omitted) whenever it cannot be confirmed present as readable UTF-8
    text at that exact commit -- deleted, renamed away, binary/
    non-UTF-8 content, or the fetch itself failed. Callers
    (:mod:`patchfrog.review_memory.evidence`) must treat ``None``
    identically to "evidence not confirmed", never as "file is empty".

    ``paths`` is used as a repo-relative git pathspec passed straight to
    ``git show``, resolved by git itself against the fetched commit's
    tree -- never touches the local filesystem outside the scratch
    checkout, so there is no path-traversal surface the way a real
    on-disk read would have.
    """

    if not paths:
        return {}

    result: dict[str, str | None] = dict.fromkeys(paths)
    root_path = Path(tempfile.mkdtemp(prefix="patchfrog-evidence-", dir=workdir_root))
    try:
        url = _inject_token(clone_url, token) if token else clone_url
        run_git(["init", "--quiet", str(root_path)], timeout_seconds=_FETCH_TIMEOUT_SECONDS)
        run_git(
            ["-C", str(root_path), "remote", "add", "origin", url],
            timeout_seconds=_FETCH_TIMEOUT_SECONDS,
        )
        try:
            run_git(
                ["-C", str(root_path), "fetch", "--quiet", "--depth", "1", "origin", commit_sha],
                timeout_seconds=_FETCH_TIMEOUT_SECONDS,
            )
        except GitError:
            run_git(
                ["-C", str(root_path), "fetch", "--quiet", "origin"],
                timeout_seconds=_FETCH_TIMEOUT_SECONDS,
            )

        for path in paths:
            try:
                result[path] = run_git(
                    ["-C", str(root_path), "show", f"{commit_sha}:{path}"],
                    timeout_seconds=_FETCH_TIMEOUT_SECONDS,
                )
            except (GitError, UnicodeDecodeError):
                result[path] = None
    except GitError:
        pass  # every path already defaults to None
    finally:
        shutil.rmtree(root_path, ignore_errors=True)

    return result
