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


def test_anchor_line_is_preserved_when_trimming_a_large_range(tmp_path: Path) -> None:
    """Regression: trimming a range that exceeds max_lines must not
    silently drop the actual line of interest (e.g. a finding deep
    inside a 600-line function) just because it isn't near the start."""

    (tmp_path / "huge.py").write_text("\n".join(f"line {i}" for i in range(1, 601)) + "\n")
    service = ContextSnippetService()

    result = service.extract(
        _snapshot(tmp_path), relative_path="huge.py", start_line=1, end_line=600, max_lines=120, anchor_line=551
    )

    assert result is not None
    assert result.truncated is True
    assert result.start_line <= 551 <= result.end_line
    assert "line 551" in result.content
    assert result.end_line - result.start_line + 1 <= 120


def test_anchor_near_start_still_includes_lead_in(tmp_path: Path) -> None:
    """When the anchor is close enough to the top that the window still
    reaches it, the lead-in (e.g. a function signature) is naturally
    included -- not sacrificed just because an anchor was supplied."""

    (tmp_path / "a.py").write_text("\n".join(f"line {i}" for i in range(1, 301)) + "\n")
    service = ContextSnippetService()

    result = service.extract(
        _snapshot(tmp_path), relative_path="a.py", start_line=1, end_line=300, max_lines=120, anchor_line=10
    )

    assert result is not None
    assert result.start_line == 1
    assert "line 1" in result.content.splitlines()[0]
    assert "line 10" in result.content


def test_anchor_window_never_exceeds_max_lines(tmp_path: Path) -> None:
    (tmp_path / "huge.py").write_text("\n".join(f"line {i}" for i in range(1, 601)) + "\n")
    service = ContextSnippetService()

    for anchor in (1, 300, 551, 600):
        result = service.extract(
            _snapshot(tmp_path), relative_path="huge.py", start_line=1, end_line=600, max_lines=50, anchor_line=anchor
        )
        assert result is not None
        assert result.end_line - result.start_line + 1 <= 50
        assert result.start_line <= anchor <= result.end_line, f"anchor {anchor} not preserved"


def test_no_anchor_keeps_default_prefix_behavior(tmp_path: Path) -> None:
    (tmp_path / "huge.py").write_text("\n".join(f"line {i}" for i in range(1, 601)) + "\n")
    service = ContextSnippetService()

    result = service.extract(_snapshot(tmp_path), relative_path="huge.py", start_line=1, end_line=600, max_lines=120)

    assert result is not None
    assert result.start_line == 1
    assert result.end_line == 120


def test_extracted_snippet_reports_actual_range_when_not_truncated(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("line1\nline2\nline3\n")
    service = ContextSnippetService()

    result = service.extract(_snapshot(tmp_path), relative_path="a.py", start_line=1, end_line=3, max_lines=100)

    assert result is not None
    assert (result.start_line, result.end_line) == (1, 3)


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
