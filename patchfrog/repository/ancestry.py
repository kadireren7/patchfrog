"""Git ancestry verification between two commits of a remote repository.

This is the mandatory safety gate for Phase 7 incremental review memory:
before any memory from a previous review is trusted, the previous
review's commit must be *proven* (via real git plumbing, never assumed)
to be an ancestor of the commit under review now. A force-push rewrites
history so the old commit is no longer reachable from the new one --
exactly the case this exists to catch.

Deliberately a *separate*, deeper fetch from
:class:`patchfrog.repository.snapshot.RepositorySnapshotProvider`'s normal
``--depth 1``-per-commit acquisition, which is intentionally too shallow
for ancestry proof: two independently shallow-fetched commits have no
shared history graph for ``git merge-base`` to walk. Ancestry verification
fetches the *full* history reachable from the descendant commit once, in
a scratch directory discarded immediately after.

Never executes anything from the checked-out tree -- this only ever runs
``git fetch``/``git merge-base`` (both disable hooks; see
:mod:`patchfrog.repository.git`).
"""

from __future__ import annotations

import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from patchfrog.indexing.incremental import parse_name_status_diff
from patchfrog.indexing.models import ChangeSet
from patchfrog.repository.git import GitError, git_is_ancestor, run_git

_FETCH_TIMEOUT_SECONDS = 300.0
_RENAME_SIMILARITY_THRESHOLD = "50%"


@dataclass(frozen=True, slots=True)
class AncestryCheckResult:
    """The outcome of attempting to prove ``ancestor_sha`` is an ancestor
    of ``descendant_sha``.

    ``is_ancestor`` is only meaningful when ``verified`` is ``True`` --
    a failed/inconclusive check (network error, unknown object, missing
    commit) must never be silently treated as "not an ancestor" *or* "is
    an ancestor"; callers must branch on ``verified`` first. Both an
    unverifiable check and a verified-false result lead to the same
    place in :mod:`patchfrog.review_memory` -- no incremental reuse --
    but the persisted reason differs (see
    :mod:`patchfrog.review_memory.domain`'s ``TransitionReasonCode``).
    """

    verified: bool
    is_ancestor: bool
    detail: str


def _inject_token(clone_url: str, token: str) -> str:
    parsed = urlsplit(clone_url)
    netloc = f"x-access-token:{token}@{parsed.hostname}"
    if parsed.port:
        netloc += f":{parsed.port}"
    return urlunsplit(parsed._replace(netloc=netloc))


def verify_ancestor(
    *,
    clone_url: str,
    ancestor_sha: str,
    descendant_sha: str,
    token: str | None = None,
    workdir_root: Path | None = None,
) -> AncestryCheckResult:
    """Prove (or fail to prove) that ``ancestor_sha`` is a real git
    ancestor of ``descendant_sha`` on ``clone_url``. See
    :func:`verify_ancestor_with_diff` for the ancestry-plus-file-diff
    variant used by :mod:`patchfrog.review_memory` -- this one exists
    on its own for callers that only need the yes/no proof."""

    result, _root_path = _fetch_and_verify(
        clone_url=clone_url, ancestor_sha=ancestor_sha, descendant_sha=descendant_sha,
        token=token, workdir_root=workdir_root,
    )
    if _root_path is not None:
        shutil.rmtree(_root_path, ignore_errors=True)
    return result


def verify_ancestor_with_diff(
    *,
    clone_url: str,
    ancestor_sha: str,
    descendant_sha: str,
    token: str | None = None,
    workdir_root: Path | None = None,
) -> tuple[AncestryCheckResult, ChangeSet | None]:
    """Prove ancestry and, only if proven, compute the tracked-file
    ``ChangeSet`` between the two commits in the *same* fetched scratch
    clone -- one fetch instead of two. ``ChangeSet`` is ``None`` whenever
    ``AncestryCheckResult`` is not a proven-ancestor outcome (unverified,
    or verified-but-not-an-ancestor): a file diff is meaningless to
    compute (and could itself be misleading) for two commits without a
    proven ancestry relationship (see :mod:`patchfrog.review_memory`'s
    "never trust memory over the current repository state" principle).
    """

    result, root_path = _fetch_and_verify(
        clone_url=clone_url, ancestor_sha=ancestor_sha, descendant_sha=descendant_sha,
        token=token, workdir_root=workdir_root,
    )
    if root_path is None or not (result.verified and result.is_ancestor):
        if root_path is not None:
            shutil.rmtree(root_path, ignore_errors=True)
        return result, None

    try:
        if ancestor_sha == descendant_sha:
            change_set = ChangeSet(old_commit_sha=ancestor_sha, new_commit_sha=descendant_sha, changes=())
        else:
            output = run_git(
                [
                    "-C", str(root_path), "diff", "--name-status",
                    f"-M{_RENAME_SIMILARITY_THRESHOLD}", "-z", ancestor_sha, descendant_sha,
                ],
                timeout_seconds=_FETCH_TIMEOUT_SECONDS,
            )
            change_set = parse_name_status_diff(output, old_commit_sha=ancestor_sha, new_commit_sha=descendant_sha)
        return result, change_set
    except GitError as exc:
        return AncestryCheckResult(verified=False, is_ancestor=False, detail=f"file diff failed: {exc}"), None
    finally:
        shutil.rmtree(root_path, ignore_errors=True)


def _fetch_and_verify(
    *,
    clone_url: str,
    ancestor_sha: str,
    descendant_sha: str,
    token: str | None,
    workdir_root: Path | None,
) -> tuple[AncestryCheckResult, Path | None]:
    """Shared fetch-then-``merge-base --is-ancestor`` core for
    :func:`verify_ancestor`/:func:`verify_ancestor_with_diff`. Returns the
    scratch directory (still on disk, caller's responsibility to remove)
    alongside the result whenever a fetch actually happened, so
    :func:`verify_ancestor_with_diff` can reuse it for a diff without a
    second network round-trip; ``None`` when nothing was fetched (the
    identical-commit short-circuit) or the fetch/verify itself failed
    before a usable clone existed.
    """

    if ancestor_sha == descendant_sha:
        return AncestryCheckResult(verified=True, is_ancestor=True, detail="identical commit"), None

    root_path = Path(tempfile.mkdtemp(prefix="patchfrog-ancestry-", dir=workdir_root))
    try:
        url = _inject_token(clone_url, token) if token else clone_url
        run_git(["init", "--quiet", str(root_path)], timeout_seconds=_FETCH_TIMEOUT_SECONDS)
        run_git(
            ["-C", str(root_path), "remote", "add", "origin", url],
            timeout_seconds=_FETCH_TIMEOUT_SECONDS,
        )
        try:
            # Full (unbounded-depth) fetch of the descendant's reachable
            # history -- deliberately no --depth here.
            run_git(
                ["-C", str(root_path), "fetch", "--quiet", "origin", descendant_sha],
                timeout_seconds=_FETCH_TIMEOUT_SECONDS,
            )
        except GitError:
            # Some servers only allow fetching branch/tag refs, not an
            # arbitrary SHA directly -- fall back to fetching everything.
            run_git(
                ["-C", str(root_path), "fetch", "--quiet", "origin"],
                timeout_seconds=_FETCH_TIMEOUT_SECONDS,
            )

        try:
            is_ancestor = git_is_ancestor(
                ancestor_sha=ancestor_sha, descendant_sha=descendant_sha, cwd=root_path
            )
        except GitError as exc:
            # Most commonly: ancestor_sha itself was never reachable from
            # descendant_sha's history at all (never shared an ancestor,
            # or belongs to a since-rewritten/deleted ref) -- git reports
            # this as "unknown revision", not a clean exit-1 "false".
            return (
                AncestryCheckResult(verified=False, is_ancestor=False, detail=f"could not resolve ancestor commit: {exc}"),
                root_path,
            )

        return (
            AncestryCheckResult(
                verified=True, is_ancestor=is_ancestor,
                detail="proven ancestor" if is_ancestor else "proven NOT an ancestor (history diverged/rewritten)",
            ),
            root_path,
        )
    except GitError as exc:
        shutil.rmtree(root_path, ignore_errors=True)
        return AncestryCheckResult(verified=False, is_ancestor=False, detail=f"ancestry check failed: {exc}"), None
