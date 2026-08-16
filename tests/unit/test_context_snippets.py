from __future__ import annotations

from pathlib import Path

from patchfrog.context.snippets import (
    MAX_FILE_READ_BYTES,
    ContextSnippetService,
    UnsafeSnippetPathError,
)
from patchfrog.repository.snapshot import RepositorySnapshot


def _snapshot(root: Path) -> RepositorySnapshot:
    return RepositorySnapshot(repository_full_name="test/repo", commit_sha="abc123", root_path=root, owns_root=False)


def test_extracts_exact_line_range(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("line1\nline2\nline3\nline4\nline5\n")
    service = ContextSnippetService()

    result = service.extract(
        _snapshot(tmp_path), relative_path="a.py", start_line=2, end_line=4, max_lines=100
    )

    assert result is not None
    assert result.content == "line2\nline3\nline4"
    assert result.truncated is False


def test_truncates_when_range_exceeds_max_lines(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("\n".join(f"line{i}" for i in range(1, 21)) + "\n")
    service = ContextSnippetService()

    result = service.extract(_snapshot(tmp_path), relative_path="a.py", start_line=1, end_line=20, max_lines=5)

    assert result is not None
    assert result.content == "line1\nline2\nline3\nline4\nline5"
    assert result.truncated is True


def test_missing_file_returns_none(tmp_path: Path) -> None:
    service = ContextSnippetService()
    result = service.extract(_snapshot(tmp_path), relative_path="nope.py", start_line=1, end_line=5, max_lines=10)
    assert result is None


def test_directory_path_returns_none(tmp_path: Path) -> None:
    (tmp_path / "somedir").mkdir()
    service = ContextSnippetService()
    result = service.extract(_snapshot(tmp_path), relative_path="somedir", start_line=1, end_line=5, max_lines=10)
    assert result is None


def test_start_line_beyond_file_length_returns_none(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("only one line\n")
    service = ContextSnippetService()
    result = service.extract(_snapshot(tmp_path), relative_path="a.py", start_line=50, end_line=60, max_lines=10)
    assert result is None


def test_end_line_beyond_file_length_is_clamped(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("line1\nline2\n")
    service = ContextSnippetService()
    result = service.extract(_snapshot(tmp_path), relative_path="a.py", start_line=1, end_line=100, max_lines=100)
    assert result is not None
    assert result.content == "line1\nline2"


def test_binary_content_returns_none(tmp_path: Path) -> None:
    (tmp_path / "bin.dat").write_bytes(b"\x00\x01\x02\x03binary\x00data")
    service = ContextSnippetService()
    result = service.extract(_snapshot(tmp_path), relative_path="bin.dat", start_line=1, end_line=5, max_lines=10)
    assert result is None


def test_malformed_encoding_returns_none(tmp_path: Path) -> None:
    (tmp_path / "bad_encoding.py").write_bytes(b"x = '\xff\xfe invalid utf8'\n")
    service = ContextSnippetService()
    result = service.extract(
        _snapshot(tmp_path), relative_path="bad_encoding.py", start_line=1, end_line=1, max_lines=10
    )
    assert result is None


def test_huge_file_beyond_read_cap_returns_none(tmp_path: Path) -> None:
    (tmp_path / "huge.py").write_bytes(b"a" * (MAX_FILE_READ_BYTES + 1))
    service = ContextSnippetService()
    result = service.extract(_snapshot(tmp_path), relative_path="huge.py", start_line=1, end_line=1, max_lines=10)
    assert result is None


def test_path_traversal_raises_unsafe_error(tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside_secret.txt"
    outside.write_text("secret\n")
    try:
        service = ContextSnippetService()
        try:
            service.extract(
                _snapshot(tmp_path), relative_path="../outside_secret.txt", start_line=1, end_line=1, max_lines=10
            )
            raise AssertionError("expected UnsafeSnippetPathError")
        except UnsafeSnippetPathError:
            pass
    finally:
        outside.unlink(missing_ok=True)


def test_absolute_path_is_rejected_as_unsafe(tmp_path: Path) -> None:
    """An absolute-looking relative_path must never let the caller read
    an arbitrary filesystem path -- pathlib's ``/`` operator discards the
    base when the right side is absolute, so a naive join would silently
    resolve outside the repo root entirely. resolve_path() catches this
    and it must surface as unsafe, not as "file not found"."""

    (tmp_path / "etc").mkdir()
    (tmp_path / "etc" / "passwd").write_text("not the real one\n")
    service = ContextSnippetService()

    try:
        service.extract(_snapshot(tmp_path), relative_path="/etc/passwd", start_line=1, end_line=1, max_lines=10)
        raise AssertionError("expected UnsafeSnippetPathError")
    except UnsafeSnippetPathError:
        pass


def test_symlink_escape_is_rejected(tmp_path: Path) -> None:
    outside_dir = tmp_path.parent / "context_snippet_symlink_target"
    outside_dir.mkdir(exist_ok=True)
    (outside_dir / "secret.txt").write_text("top secret\n")
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    symlink_path = repo_root / "escape.txt"
    try:
        symlink_path.symlink_to(outside_dir / "secret.txt")
        service = ContextSnippetService()
        try:
            service.extract(
                _snapshot(repo_root), relative_path="escape.txt", start_line=1, end_line=1, max_lines=10
            )
            raise AssertionError("expected UnsafeSnippetPathError")
        except UnsafeSnippetPathError:
            pass
    finally:
        symlink_path.unlink(missing_ok=True)
        import shutil

        shutil.rmtree(outside_dir, ignore_errors=True)


def test_symlink_within_repo_root_is_followed_safely(tmp_path: Path) -> None:
    """A symlink whose target stays inside the repo root isn't a security
    escape -- resolve_path() fully resolves it, and since the result is
    still within the root, it's read like any other in-repo file. Only a
    symlink that resolves *outside* the root is rejected (see
    test_symlink_escape_is_rejected)."""

    (tmp_path / "real.py").write_text("value = 1\n")
    (tmp_path / "alias.py").symlink_to(tmp_path / "real.py")
    service = ContextSnippetService()

    result = service.extract(_snapshot(tmp_path), relative_path="alias.py", start_line=1, end_line=1, max_lines=10)

    assert result is not None
    assert result.content == "value = 1"


def test_unicode_and_spaces_in_filename(tmp_path: Path) -> None:
    weird_name = "café módülé — тест (1).py"
    (tmp_path / weird_name).write_text("value = 1\n")
    service = ContextSnippetService()

    result = service.extract(_snapshot(tmp_path), relative_path=weird_name, start_line=1, end_line=1, max_lines=10)

    assert result is not None
    assert result.content == "value = 1"
