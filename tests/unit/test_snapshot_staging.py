"""Unit tests for patchfrog.executable_verification.snapshot_staging --
the credential-free artifact export (git archive, never .git/) and its
content-manifest digest, against real git repositories, real tampering,
real synthetic-credential reproductions -- never a hand-built stand-in.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from patchfrog.executable_verification.snapshot_staging import (
    ArtifactExportError,
    compute_artifact_digest,
    export_artifact,
)
from patchfrog.repository.git import run_git

_SYNTHETIC_TOKEN = "ghs_SYNTHETIC_SENTINEL_TOKEN_DO_NOT_USE_1234567890"


def _init_bare_remote(tmp_path: Path) -> Path:
    remote = tmp_path / "remote.git"
    run_git(["init", "--quiet", "--bare", str(remote)])
    return remote


def _push_commit(remote: Path, tmp_path: Path, *, files: dict[str, str], symlinks: dict[str, str] | None = None) -> str:
    work = tmp_path / "work"
    run_git(["clone", "--quiet", str(remote), str(work)])
    run_git(["-C", str(work), "config", "user.email", "t@example.com"])
    run_git(["-C", str(work), "config", "user.name", "T"])
    for rel, content in files.items():
        path = work / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    for rel, target in (symlinks or {}).items():
        path = work / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.symlink_to(target)
    run_git(["-C", str(work), "add", "-A"])
    run_git(["-C", str(work), "commit", "--quiet", "-m", "commit"])
    run_git(["-C", str(work), "push", "--quiet", "origin", "HEAD:refs/heads/main"])
    return run_git(["-C", str(work), "rev-parse", "HEAD"]).strip()


def test_export_artifact_contains_no_git_directory(tmp_path: Path) -> None:
    remote = _init_bare_remote(tmp_path)
    sha = _push_commit(remote, tmp_path, files={"tests/test_x.py": "def test_pass():\n    assert 1 == 1\n"})
    staging_root = tmp_path / "staging"
    staging_root.mkdir()

    artifact_dir = export_artifact(
        clone_url=f"file://{remote}", commit_sha=sha, repository_full_name="test/repo",
        token=_SYNTHETIC_TOKEN, destination_root=staging_root,
    )

    assert not (artifact_dir / ".git").exists()


def test_export_artifact_never_leaks_synthetic_credential_recursively(tmp_path: Path) -> None:
    """The exact security-correction regression: a synthetic GitHub token
    used only during trusted acquisition must never appear anywhere in
    the exported, verifier-visible artifact -- checked recursively across
    every file, not merely the obvious `.git/config` location."""

    remote = _init_bare_remote(tmp_path)
    sha = _push_commit(remote, tmp_path, files={"tests/test_x.py": "def test_pass():\n    assert 1 == 1\n"})
    staging_root = tmp_path / "staging"
    staging_root.mkdir()

    artifact_dir = export_artifact(
        clone_url=f"file://{remote}", commit_sha=sha, repository_full_name="test/repo",
        token=_SYNTHETIC_TOKEN, destination_root=staging_root,
    )

    for root, _dirs, files in os.walk(artifact_dir):
        for name in files:
            path = Path(root) / name
            if path.is_symlink():
                continue
            content = path.read_bytes()
            assert _SYNTHETIC_TOKEN.encode() not in content, f"synthetic credential leaked into {path}"


def test_export_artifact_preserves_symlinks_without_dereferencing(tmp_path: Path) -> None:
    remote = _init_bare_remote(tmp_path)
    outside = tmp_path / "outside_secret.txt"
    outside.write_text("SENTINEL_OUTSIDE_CONTENT")
    sha = _push_commit(
        remote, tmp_path,
        files={"tests/test_x.py": "def test_pass():\n    assert 1 == 1\n"},
        symlinks={"tests/evil_link.txt": str(outside)},
    )
    staging_root = tmp_path / "staging"
    staging_root.mkdir()

    artifact_dir = export_artifact(
        clone_url=f"file://{remote}", commit_sha=sha, repository_full_name="test/repo",
        token=_SYNTHETIC_TOKEN, destination_root=staging_root,
    )

    link = artifact_dir / "tests" / "evil_link.txt"
    assert link.is_symlink()
    assert str(link.readlink()) == str(outside)


def test_export_artifact_raises_on_bad_token_or_unreachable_remote(tmp_path: Path) -> None:
    staging_root = tmp_path / "staging"
    staging_root.mkdir()
    with pytest.raises(ArtifactExportError):
        export_artifact(
            clone_url="file:///nonexistent/remote/path", commit_sha="a" * 40, repository_full_name="test/repo",
            token=_SYNTHETIC_TOKEN, destination_root=staging_root,
        )


def test_compute_artifact_digest_deterministic(tmp_path: Path) -> None:
    root = tmp_path / "artifact"
    root.mkdir()
    (root / "a.py").write_text("content")
    assert compute_artifact_digest(root) == compute_artifact_digest(root)


def test_compute_artifact_digest_changes_on_file_content_tamper(tmp_path: Path) -> None:
    root = tmp_path / "artifact"
    root.mkdir()
    (root / "a.py").write_text("original")
    before = compute_artifact_digest(root)
    (root / "a.py").write_text("tampered")
    after = compute_artifact_digest(root)
    assert before != after


def test_compute_artifact_digest_changes_on_untracked_file_injection(tmp_path: Path) -> None:
    """An injected extra file must change the digest -- this is the exact
    property git diff-index (tracked-content-only) never provided."""

    root = tmp_path / "artifact"
    root.mkdir()
    (root / "a.py").write_text("content")
    before = compute_artifact_digest(root)
    (root / "b_injected.py").write_text("injected")
    after = compute_artifact_digest(root)
    assert before != after


def test_compute_artifact_digest_changes_on_symlink_target_tamper(tmp_path: Path) -> None:
    root = tmp_path / "artifact"
    root.mkdir()
    (root / "link.txt").symlink_to("/tmp/original-target")
    before = compute_artifact_digest(root)
    (root / "link.txt").unlink()
    (root / "link.txt").symlink_to("/tmp/different-target")
    after = compute_artifact_digest(root)
    assert before != after


def test_compute_artifact_digest_represents_directory_symlink() -> None:
    """A symlinked directory appears in os.walk's dirnames, not
    filenames, and os.walk never descends into it -- it must still be
    represented in the digest, or a malicious directory-symlink would be
    invisible to integrity checking while still being faithfully
    preserved (and reachable) by a symlinks=True copy."""

    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "artifact"
        root.mkdir()
        (root / "evil_dir_link").symlink_to("/etc")
        digest_with_link = compute_artifact_digest(root)

        (root / "evil_dir_link").unlink()
        digest_without_link = compute_artifact_digest(root)

    assert digest_with_link != digest_without_link


def test_compute_artifact_digest_changes_on_symlink_vs_real_directory() -> None:
    """A directory symlink and a real directory with the same name must
    not collide in the digest -- the manifest must distinguish entry
    kind, not just presence."""

    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        symlink_root = Path(tmp) / "with_symlink"
        symlink_root.mkdir()
        (symlink_root / "d").symlink_to("/etc")
        symlink_digest = compute_artifact_digest(symlink_root)

        real_root = Path(tmp) / "with_real_dir"
        real_root.mkdir()
        (real_root / "d").mkdir()
        real_digest = compute_artifact_digest(real_root)

    assert symlink_digest != real_digest
