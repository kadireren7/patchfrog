from __future__ import annotations

from pathlib import Path

import pytest

from patchfrog.repository.snapshot import RepositorySnapshot


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
