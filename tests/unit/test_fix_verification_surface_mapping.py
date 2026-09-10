from __future__ import annotations

from pathlib import Path

from patchfrog.domain.code import Language
from patchfrog.fix_verification.surface_mapping import SurfaceMappingStatus, map_finding_surface
from tests.support.git_repo import commit_all, init_git_repo


def _make_repo_with_two_commits(tmp_path: Path, *, original: str, candidate: str) -> tuple[Path, str]:
    root = tmp_path / "repo"
    root.mkdir()
    init_git_repo(root)
    (root / "m.py").write_text(original)
    original_sha = commit_all(root, "original")
    (root / "m.py").write_text(candidate)
    commit_all(root, "candidate")
    return root, original_sha


def test_map_finding_surface_unchanged_when_symbol_body_identical(tmp_path: Path) -> None:
    root, original_sha = _make_repo_with_two_commits(
        tmp_path,
        original="def f():\n    return 1\n\n\ndef unrelated():\n    return 2\n",
        candidate="def f():\n    return 1\n\n\ndef unrelated():\n    return 3\n",
    )
    result = map_finding_surface(
        candidate_checkout=root, original_commit_sha=original_sha, file_path="m.py",
        qualified_name="f", language=Language.PYTHON,
    )
    assert result.status is SurfaceMappingStatus.UNCHANGED
    assert result.file_path == "m.py"


def test_map_finding_surface_modified_when_symbol_body_changed(tmp_path: Path) -> None:
    root, original_sha = _make_repo_with_two_commits(
        tmp_path, original="def f():\n    return undefined_name\n", candidate="def f():\n    return 1\n",
    )
    result = map_finding_surface(
        candidate_checkout=root, original_commit_sha=original_sha, file_path="m.py",
        qualified_name="f", language=Language.PYTHON,
    )
    assert result.status is SurfaceMappingStatus.MODIFIED
    assert result.file_path == "m.py"
    assert result.start_line is not None


def test_map_finding_surface_moved_within_file_by_identical_body(tmp_path: Path) -> None:
    """A method's own signature/body span never includes its enclosing
    class's name -- so moving it to a *different* class (a different
    qualified_name) while keeping its own body byte-for-byte identical is
    exactly the content-hash rename/move case, unlike a bare top-level
    function rename (which changes its own ``def <name>`` line and so
    never matches by content hash -- see the "no body match" test
    below)."""

    root, original_sha = _make_repo_with_two_commits(
        tmp_path,
        original="class A:\n    def method(self):\n        return undefined_name\n",
        candidate=(
            "class B:\n    def method(self):\n        return undefined_name\n\n\n"
            "class A:\n    def unrelated(self):\n        return 2\n"
        ),
    )
    result = map_finding_surface(
        candidate_checkout=root, original_commit_sha=original_sha, file_path="m.py",
        qualified_name="A.method", language=Language.PYTHON,
    )
    assert result.status is SurfaceMappingStatus.MOVED_OR_RENAMED
    assert result.file_path == "m.py"


def test_map_finding_surface_unmappable_when_file_deleted(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    init_git_repo(root)
    (root / "m.py").write_text("def f():\n    return undefined_name\n")
    original_sha = commit_all(root, "original")
    (root / "m.py").unlink()
    commit_all(root, "delete m.py")

    result = map_finding_surface(
        candidate_checkout=root, original_commit_sha=original_sha, file_path="m.py",
        qualified_name="f", language=Language.PYTHON,
    )
    assert result.status is SurfaceMappingStatus.UNMAPPABLE
    assert not result.is_safely_mapped


def test_map_finding_surface_unmappable_when_symbol_disappears_with_no_body_match(tmp_path: Path) -> None:
    root, original_sha = _make_repo_with_two_commits(
        tmp_path, original="def f():\n    return undefined_name\n", candidate="def g():\n    return 42\n",
    )
    result = map_finding_surface(
        candidate_checkout=root, original_commit_sha=original_sha, file_path="m.py",
        qualified_name="f", language=Language.PYTHON,
    )
    assert result.status is SurfaceMappingStatus.UNMAPPABLE


def test_map_finding_surface_ambiguous_when_two_candidates_share_identical_body(tmp_path: Path) -> None:
    root, original_sha = _make_repo_with_two_commits(
        tmp_path,
        original="class A:\n    def method(self):\n        return undefined_name\n",
        candidate=(
            "class B:\n    def method(self):\n        return undefined_name\n\n\n"
            "class C:\n    def method(self):\n        return undefined_name\n"
        ),
    )
    result = map_finding_surface(
        candidate_checkout=root, original_commit_sha=original_sha, file_path="m.py",
        qualified_name="A.method", language=Language.PYTHON,
    )
    assert result.status is SurfaceMappingStatus.AMBIGUOUS
    assert not result.is_safely_mapped


def test_map_finding_surface_unmappable_without_qualified_name(tmp_path: Path) -> None:
    root, original_sha = _make_repo_with_two_commits(
        tmp_path, original="x = 1\n", candidate="x = 2\n",
    )
    result = map_finding_surface(
        candidate_checkout=root, original_commit_sha=original_sha, file_path="m.py",
        qualified_name=None, language=Language.PYTHON,
    )
    assert result.status is SurfaceMappingStatus.UNMAPPABLE


def test_map_finding_surface_unmappable_without_language(tmp_path: Path) -> None:
    root, original_sha = _make_repo_with_two_commits(
        tmp_path, original="def f():\n    return 1\n", candidate="def f():\n    return 2\n",
    )
    result = map_finding_surface(
        candidate_checkout=root, original_commit_sha=original_sha, file_path="m.py",
        qualified_name="f", language=None,
    )
    assert result.status is SurfaceMappingStatus.UNMAPPABLE


def test_map_finding_surface_unmappable_when_original_blob_unreachable(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    init_git_repo(root)
    (root / "m.py").write_text("def f():\n    return 1\n")
    commit_all(root, "only commit")

    result = map_finding_surface(
        candidate_checkout=root, original_commit_sha="f" * 40, file_path="m.py",
        qualified_name="f", language=Language.PYTHON,
    )
    assert result.status is SurfaceMappingStatus.UNMAPPABLE
