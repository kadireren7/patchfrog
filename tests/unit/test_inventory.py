from __future__ import annotations

from pathlib import Path

from patchfrog.domain.code import Language
from patchfrog.indexing.inventory import build_inventory, is_test_path
from tests.support.git_repo import commit_all, init_git_repo, snapshot_at_head


def test_is_test_path_matches_common_conventions() -> None:
    assert is_test_path("tests/test_cache.py")
    assert is_test_path("src/cache_test.c")
    assert is_test_path("src/test_cache.c")
    assert is_test_path("src/CacheTest.cpp") is False  # no separator before "Test" — not matched, intentionally conservative
    assert is_test_path("src/cache.py") is False


def test_inventory_only_includes_git_tracked_files(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "tracked.py").write_text("def fn():\n    pass\n")
    init_git_repo(root)
    commit_sha = commit_all(root, "initial")  # commits only tracked.py
    (root / "untracked.py").write_text("def other():\n    pass\n")  # never `git add`ed

    snapshot = snapshot_at_head(root, "test/repo")
    assert snapshot.commit_sha == commit_sha
    paths = {e.relative_path for e in build_inventory(snapshot)}
    assert paths == {"tracked.py"}


def test_inventory_skips_denylisted_directories(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "src").mkdir()
    (root / "src" / "main.py").write_text("x = 1\n")
    (root / "node_modules" / "pkg").mkdir(parents=True)
    (root / "node_modules" / "pkg" / "index.py").write_text("y = 2\n")
    init_git_repo(root)
    # Force-add node_modules despite any .gitignore, to prove the denylist
    # (not just .gitignore) is what excludes it.
    from patchfrog.repository.git import run_git

    run_git(["-C", str(root), "add", "-f", "-A"])
    run_git(["-C", str(root), "commit", "--quiet", "-m", "initial"])

    snapshot = snapshot_at_head(root, "test/repo")
    paths = {e.relative_path for e in build_inventory(snapshot)}
    assert paths == {"src/main.py"}


def test_inventory_records_language_size_and_hash(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "main.py").write_text("x = 1\n")
    init_git_repo(root)
    commit_all(root, "initial")

    snapshot = snapshot_at_head(root, "test/repo")
    entry = build_inventory(snapshot)[0]
    assert entry.language is Language.PYTHON
    assert entry.size_bytes == len(b"x = 1\n")
    assert len(entry.content_hash) == 64
    assert entry.git_blob_sha is not None


def test_inventory_marks_generated_files() -> None:
    from patchfrog.indexing.inventory import _looks_generated

    assert _looks_generated(b"// DO NOT EDIT: this file is auto-generated\nint x;\n")
    assert not _looks_generated(b"int x = 1;\n")
