"""Real-git-plumbing coverage for :func:`patchfrog.repository.file_contents.read_files_at_commit`
-- the exact-current-commit read Phase 7 evidence revalidation relies on
(never a stale checkout, never a previously-built ContextBundle)."""

from __future__ import annotations

from pathlib import Path

from patchfrog.repository.file_contents import read_files_at_commit
from tests.support.git_repo import commit_all, init_git_repo


def _init_repo(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    init_git_repo(root)


def test_reads_exact_file_content_at_commit(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    _init_repo(root)
    (root / "a.py").write_text("def foo():\n    return 1\n")
    sha1 = commit_all(root, "c1")
    (root / "a.py").write_text("def foo():\n    return 2\n")
    sha2 = commit_all(root, "c2")

    result1 = read_files_at_commit(clone_url=str(root), commit_sha=sha1, paths=frozenset({"a.py"}))
    assert result1["a.py"] == "def foo():\n    return 1\n"

    result2 = read_files_at_commit(clone_url=str(root), commit_sha=sha2, paths=frozenset({"a.py"}))
    assert result2["a.py"] == "def foo():\n    return 2\n"


def test_multiple_paths_in_one_call(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    _init_repo(root)
    (root / "a.py").write_text("A\n")
    (root / "b.py").write_text("B\n")
    sha = commit_all(root, "c1")

    result = read_files_at_commit(clone_url=str(root), commit_sha=sha, paths=frozenset({"a.py", "b.py"}))
    assert result == {"a.py": "A\n", "b.py": "B\n"}


def test_nonexistent_path_is_none(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    _init_repo(root)
    (root / "a.py").write_text("A\n")
    sha = commit_all(root, "c1")

    result = read_files_at_commit(clone_url=str(root), commit_sha=sha, paths=frozenset({"does_not_exist.py"}))
    assert result["does_not_exist.py"] is None


def test_file_deleted_at_that_commit_is_none(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    _init_repo(root)
    (root / "a.py").write_text("A\n")
    commit_all(root, "add a.py")
    (root / "a.py").unlink()
    sha2 = commit_all(root, "delete a.py")

    result = read_files_at_commit(clone_url=str(root), commit_sha=sha2, paths=frozenset({"a.py"}))
    assert result["a.py"] is None


def test_empty_paths_returns_empty_without_any_fetch(tmp_path: Path) -> None:
    result = read_files_at_commit(clone_url=str(tmp_path / "unreachable"), commit_sha="a" * 40, paths=frozenset())
    assert result == {}


def test_unreachable_clone_url_returns_none_for_every_path_not_raising(tmp_path: Path) -> None:
    result = read_files_at_commit(
        clone_url=str(tmp_path / "does-not-exist"), commit_sha="a" * 40, paths=frozenset({"a.py"})
    )
    assert result == {"a.py": None}
