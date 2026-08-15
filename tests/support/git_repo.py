"""Test helper: materialize a fixture tree as a real, throwaway git repo.

Fixture repos under ``tests/fixtures/repos/`` are plain file trees (no
``.git`` — a nested repo checked into the main repo is awkward and easy
to break). Tests that need :func:`patchfrog.indexing.inventory.build_inventory`
(which shells out to ``git ls-files``) or incremental diffing copy a
fixture into a ``tmp_path`` and git-init it here instead.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from patchfrog.repository.git import run_git
from patchfrog.repository.snapshot import RepositorySnapshot

FIXTURES_REPOS_DIR = Path(__file__).parent.parent / "fixtures" / "repos"


def init_git_repo(root: Path) -> None:
    run_git(["-C", str(root), "init", "--quiet"])
    run_git(["-C", str(root), "config", "user.email", "test@patchfrog.dev"])
    run_git(["-C", str(root), "config", "user.name", "PatchFrog Test"])


def commit_all(root: Path, message: str) -> str:
    """Stage everything and commit, returning the new commit SHA."""

    run_git(["-C", str(root), "add", "-A"])
    run_git(["-C", str(root), "commit", "--quiet", "-m", message])
    return run_git(["-C", str(root), "rev-parse", "HEAD"]).strip()


def snapshot_at_head(root: Path, full_name: str) -> RepositorySnapshot:
    commit_sha = run_git(["-C", str(root), "rev-parse", "HEAD"]).strip()
    return RepositorySnapshot(
        repository_full_name=full_name, commit_sha=commit_sha, root_path=root, owns_root=False
    )


def materialize_fixture_repo(
    dest: Path, fixture_name: str, *, full_name: str | None = None
) -> RepositorySnapshot:
    """Copy ``tests/fixtures/repos/<fixture_name>`` into ``dest``, commit it, and
    return a :class:`RepositorySnapshot` pointing at the resulting HEAD."""

    shutil.copytree(FIXTURES_REPOS_DIR / fixture_name, dest)
    init_git_repo(dest)
    commit_all(dest, "initial")
    return snapshot_at_head(dest, full_name or f"test/{fixture_name}")
