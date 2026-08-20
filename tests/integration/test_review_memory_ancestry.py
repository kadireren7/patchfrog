"""Real-git-plumbing coverage for :mod:`patchfrog.repository.ancestry` --
force-push/history-rewrite detection, unrelated-history detection, and
the combined ancestry+diff fetch, against real throwaway local git
repositories (git supports a local filesystem path as a remote directly,
no server needed)."""

from __future__ import annotations

from pathlib import Path

from patchfrog.indexing.models import FileChangeType
from patchfrog.repository.ancestry import verify_ancestor, verify_ancestor_with_diff
from patchfrog.repository.git import run_git
from tests.support.git_repo import commit_all, init_git_repo


def _init_repo(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    init_git_repo(root)


def test_direct_ancestor_is_verified(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    _init_repo(root)
    (root / "a.txt").write_text("1\n")
    sha1 = commit_all(root, "c1")
    (root / "a.txt").write_text("2\n")
    sha2 = commit_all(root, "c2")

    result = verify_ancestor(clone_url=str(root), ancestor_sha=sha1, descendant_sha=sha2)
    assert result.verified is True
    assert result.is_ancestor is True


def test_identical_commit_is_trivially_an_ancestor(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    _init_repo(root)
    (root / "a.txt").write_text("1\n")
    sha1 = commit_all(root, "c1")

    result = verify_ancestor(clone_url=str(root), ancestor_sha=sha1, descendant_sha=sha1)
    assert result.verified is True
    assert result.is_ancestor is True


def test_force_pushed_history_is_verified_but_not_an_ancestor(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    _init_repo(root)
    (root / "a.txt").write_text("1\n")
    sha1 = commit_all(root, "c1")
    (root / "a.txt").write_text("2\n")
    old_head = commit_all(root, "c2 (will be rewritten)")

    # Simulate a force-push: reset to sha1, then commit different content
    # -- old_head is no longer reachable from the new HEAD at all.
    run_git(["-C", str(root), "reset", "--hard", sha1])
    (root / "a.txt").write_text("3\n")
    new_head = commit_all(root, "c2 (rewritten)")
    assert new_head != old_head

    result = verify_ancestor(clone_url=str(root), ancestor_sha=old_head, descendant_sha=new_head)
    # A rewritten commit is provably NOT reachable -- but git may report
    # this either as a clean "not an ancestor" (verified=True,
    # is_ancestor=False) or as "unknown revision" (verified=False)
    # depending on whether the old object is still locally dangling.
    # Both are safe, both mean "no incremental reuse" -- assert on the
    # actual contract: never verified=True and is_ancestor=True.
    assert not (result.verified and result.is_ancestor)


def test_unrelated_histories_are_not_ancestors(tmp_path: Path) -> None:
    repo_a = tmp_path / "repo_a"
    _init_repo(repo_a)
    (repo_a / "a.txt").write_text("1\n")
    sha_a = commit_all(repo_a, "a")

    repo_b = tmp_path / "repo_b"
    _init_repo(repo_b)
    (repo_b / "b.txt").write_text("1\n")
    sha_b = commit_all(repo_b, "b")

    result = verify_ancestor(clone_url=str(repo_b), ancestor_sha=sha_a, descendant_sha=sha_b)
    assert not (result.verified and result.is_ancestor)


def test_verify_ancestor_with_diff_returns_real_file_changes(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    _init_repo(root)
    (root / "a.py").write_text("def foo():\n    pass\n")
    sha1 = commit_all(root, "c1")
    (root / "a.py").write_text("def foo():\n    return 1\n")
    (root / "b.py").write_text("def bar():\n    pass\n")
    sha2 = commit_all(root, "c2")

    result, change_set = verify_ancestor_with_diff(clone_url=str(root), ancestor_sha=sha1, descendant_sha=sha2)
    assert result.verified is True
    assert result.is_ancestor is True
    assert change_set is not None
    assert change_set.old_commit_sha == sha1
    assert change_set.new_commit_sha == sha2
    paths_by_type = {c.path: c.change_type for c in change_set.changes}
    assert paths_by_type["a.py"] is FileChangeType.MODIFIED
    assert paths_by_type["b.py"] is FileChangeType.ADDED


def test_verify_ancestor_with_diff_returns_none_when_not_an_ancestor(tmp_path: Path) -> None:
    repo_a = tmp_path / "repo_a"
    _init_repo(repo_a)
    (repo_a / "a.txt").write_text("1\n")
    sha_a = commit_all(repo_a, "a")

    repo_b = tmp_path / "repo_b"
    _init_repo(repo_b)
    (repo_b / "b.txt").write_text("1\n")
    sha_b = commit_all(repo_b, "b")

    result, change_set = verify_ancestor_with_diff(clone_url=str(repo_b), ancestor_sha=sha_a, descendant_sha=sha_b)
    assert not (result.verified and result.is_ancestor)
    assert change_set is None


def test_unreachable_clone_url_is_unverifiable_not_raising(tmp_path: Path) -> None:
    result = verify_ancestor(
        clone_url=str(tmp_path / "does-not-exist"), ancestor_sha="a" * 40, descendant_sha="b" * 40
    )
    assert result.verified is False
    assert result.is_ancestor is False
