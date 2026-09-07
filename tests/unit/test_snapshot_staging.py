"""Unit tests for patchfrog.executable_verification.snapshot_staging's
integrity check -- against real git repositories, real tampering, never a
hand-built stand-in. Confirms both halves of the check: identity
(rev-parse HEAD) and content (diff-index --quiet HEAD --)."""

from __future__ import annotations

from pathlib import Path

from patchfrog.executable_verification.snapshot_staging import verify_snapshot_integrity
from patchfrog.repository.git import run_git


def _init_repo(tmp_path: Path) -> tuple[Path, str]:
    repo = tmp_path / "repo"
    repo.mkdir()
    run_git(["-C", str(repo), "init", "--quiet"])
    run_git(["-C", str(repo), "config", "user.email", "test@example.com"])
    run_git(["-C", str(repo), "config", "user.name", "Test"])
    (repo / "file.txt").write_text("original content\n")
    run_git(["-C", str(repo), "add", "file.txt"])
    run_git(["-C", str(repo), "commit", "--quiet", "-m", "initial"])
    commit_sha = run_git(["-C", str(repo), "rev-parse", "HEAD"]).strip()
    return repo, commit_sha


def test_clean_checkout_passes_integrity_check(tmp_path: Path) -> None:
    repo, commit_sha = _init_repo(tmp_path)
    assert verify_snapshot_integrity(root_path=repo, expected_commit_sha=commit_sha) is True


def test_tampered_tracked_file_fails_integrity_check(tmp_path: Path) -> None:
    repo, commit_sha = _init_repo(tmp_path)
    (repo / "file.txt").write_text("tampered content injected after staging\n")
    assert verify_snapshot_integrity(root_path=repo, expected_commit_sha=commit_sha) is False


def test_wrong_commit_sha_fails_integrity_check(tmp_path: Path) -> None:
    repo, _ = _init_repo(tmp_path)
    assert verify_snapshot_integrity(root_path=repo, expected_commit_sha="f" * 40) is False


def test_not_a_git_checkout_fails_closed(tmp_path: Path) -> None:
    not_a_repo = tmp_path / "not-a-repo"
    not_a_repo.mkdir()
    (not_a_repo / "file.txt").write_text("hello")
    assert verify_snapshot_integrity(root_path=not_a_repo, expected_commit_sha="a" * 40) is False


def test_missing_path_fails_closed(tmp_path: Path) -> None:
    missing = tmp_path / "does-not-exist"
    assert verify_snapshot_integrity(root_path=missing, expected_commit_sha="a" * 40) is False


def test_extra_committed_content_after_new_commit_fails_for_old_sha(tmp_path: Path) -> None:
    """A snapshot that has moved past the expected commit (e.g. someone
    reused the directory for a different head) must fail closed too --
    identity binding, not merely content-cleanliness."""

    repo, commit_sha = _init_repo(tmp_path)
    (repo / "file2.txt").write_text("second file\n")
    run_git(["-C", str(repo), "add", "file2.txt"])
    run_git(["-C", str(repo), "commit", "--quiet", "-m", "second commit"])
    assert verify_snapshot_integrity(root_path=repo, expected_commit_sha=commit_sha) is False
