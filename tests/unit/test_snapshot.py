from __future__ import annotations

from pathlib import Path

import pytest

from patchfrog.repository.git import GitError
from patchfrog.repository.snapshot import RepositorySnapshot, RepositorySnapshotProvider
from tests.support.git_repo import commit_all, init_git_repo


def test_resolve_path_rejects_traversal_outside_root(tmp_path: Path) -> None:
    snapshot = RepositorySnapshot(repository_full_name="test/repo", commit_sha="abc", root_path=tmp_path)

    with pytest.raises(ValueError, match="escapes repository root"):
        snapshot.resolve_path("../../etc/passwd")


def test_resolve_path_allows_normal_relative_paths(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text("x = 1\n")
    snapshot = RepositorySnapshot(repository_full_name="test/repo", commit_sha="abc", root_path=tmp_path)

    resolved = snapshot.resolve_path("src/main.py")

    assert resolved == (tmp_path / "src" / "main.py").resolve()


def test_cleanup_removes_owned_directory(tmp_path: Path) -> None:
    root = tmp_path / "owned"
    root.mkdir()
    snapshot = RepositorySnapshot(repository_full_name="test/repo", commit_sha="abc", root_path=root)

    snapshot.cleanup()

    assert not root.exists()


def test_cleanup_never_deletes_a_directory_it_does_not_own(tmp_path: Path) -> None:
    root = tmp_path / "not-owned"
    root.mkdir()
    snapshot = RepositorySnapshot(
        repository_full_name="test/repo", commit_sha="abc", root_path=root, owns_root=False
    )

    snapshot.cleanup()

    assert root.exists()


def test_context_manager_cleans_up_owned_snapshot(tmp_path: Path) -> None:
    root = tmp_path / "owned"
    root.mkdir()

    with RepositorySnapshot(repository_full_name="test/repo", commit_sha="abc", root_path=root):
        assert root.exists()

    assert not root.exists()


def test_acquire_local_refuses_a_dirty_working_tree(tmp_path: Path) -> None:
    """A local index is labeled with a commit SHA — indexing uncommitted
    changes under the last clean commit's SHA would silently mislabel it.
    """

    root = tmp_path / "repo"
    root.mkdir()
    (root / "a.py").write_text("def foo():\n    pass\n")
    init_git_repo(root)
    commit_all(root, "initial")
    (root / "a.py").write_text("def foo():\n    pass\ndef uncommitted():\n    pass\n")  # dirty

    with pytest.raises(GitError, match="dirty working tree"):
        RepositorySnapshotProvider().acquire_local(root_path=root, repository_full_name="test/repo")


def test_acquire_local_ignores_untracked_files(tmp_path: Path) -> None:
    """Untracked files never affect indexed content (see the inventory,
    which only reads `git ls-files`), so they must not block acquisition."""

    root = tmp_path / "repo"
    root.mkdir()
    (root / "a.py").write_text("def foo():\n    pass\n")
    init_git_repo(root)
    commit_sha = commit_all(root, "initial")
    (root / "untracked.py").write_text("def other():\n    pass\n")

    snapshot = RepositorySnapshotProvider().acquire_local(root_path=root, repository_full_name="test/repo")

    assert snapshot.commit_sha == commit_sha


def test_acquire_local_succeeds_on_a_clean_tree(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "a.py").write_text("def foo():\n    pass\n")
    init_git_repo(root)
    commit_sha = commit_all(root, "initial")

    snapshot = RepositorySnapshotProvider().acquire_local(root_path=root, repository_full_name="test/repo")

    assert snapshot.commit_sha == commit_sha
    assert snapshot.owns_root is False
