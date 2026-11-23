"""RepositoryRevision (Milestone 2.1): identity, scope, safety, consistency."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

import patchfrog.app.revision as revision_module
from patchfrog.app.revision import (
    RevisionBudgetExceeded,
    RevisionCaptureError,
    RevisionKind,
    RevisionScope,
    RevisionUnstableError,
    capture_revision,
)
from patchfrog.repository.git import run_git
from tests.support.git_repo import commit_all, init_git_repo


def _write(root: Path, files: dict[str, str]) -> Path:
    for relative, content in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    return root


_FILES = {"app/main.py": "print('hi')\n", "app/db.py": "x = 1\n", "README.md": "# demo\n"}


# --- identity ---------------------------------------------------------------------


def test_identity_is_independent_of_location_and_creation_order(tmp_path: Path) -> None:
    first = _write(tmp_path / "one", _FILES)
    second = _write(tmp_path / "elsewhere" / "two", dict(reversed(list(_FILES.items()))))

    a, b = capture_revision(first), capture_revision(second)

    assert a.revision_id == b.revision_id
    assert [e.path for e in a.entries] == ["README.md", "app/db.py", "app/main.py"]
    assert a.kind is RevisionKind.DIRECTORY and a.commit_sha is None and a.dirty is None


def test_repeated_capture_of_an_unchanged_tree_is_stable(tmp_path: Path) -> None:
    root = _write(tmp_path / "r", _FILES)
    ids = {capture_revision(root).revision_id for _ in range(3)}
    assert len(ids) == 1


@pytest.mark.parametrize(
    "mutate",
    [
        lambda r: (r / "app/db.py").write_text("x = 2\n"),  # same size, different bytes
        lambda r: (r / "app/new.py").write_text(""),  # empty new file
        lambda r: (r / "README.md").unlink(),
        lambda r: (r / "app/db.py").rename(r / "app/db2.py"),  # rename only
    ],
    ids=["edit-same-size", "add-empty", "delete", "rename"],
)
def test_any_relevant_change_changes_the_identity(tmp_path: Path, mutate: object) -> None:
    root = _write(tmp_path / "r", _FILES)
    before = capture_revision(root).revision_id
    mutate(root)  # type: ignore[operator]
    assert capture_revision(root).revision_id != before


def test_scope_is_part_of_the_identity_but_budgets_are_not(tmp_path: Path) -> None:
    root = _write(tmp_path / "r", _FILES)
    base = capture_revision(root)

    assert capture_revision(root, RevisionScope(exclude_prefixes=("docs",))).revision_id != base.revision_id
    assert capture_revision(root, RevisionScope(max_files=10, max_total_bytes=10_000)).revision_id == base.revision_id


def test_invalid_exclude_prefixes_are_rejected() -> None:
    for bad in ("../x", "/abs", "a/../b", "", "a\\b"):
        with pytest.raises(ValueError):
            RevisionScope(exclude_prefixes=(bad,))


def test_store_inside_the_root_is_excluded_so_identity_never_self_references(tmp_path: Path) -> None:
    root = _write(tmp_path / "r", _FILES)
    store = root / ".patchfrog-store"
    before = capture_revision(root, exclude_paths=(store,))
    _write(store, {"objects/ab/x": "artifact bytes"})

    after = capture_revision(root, exclude_paths=(store,))

    assert after.revision_id == before.revision_id
    assert after.stats.excluded_by_scope == 1
    with pytest.raises(RevisionCaptureError):
        capture_revision(root, exclude_paths=(root,))


# --- scope rules -------------------------------------------------------------------


def test_secret_and_denylisted_files_are_never_part_of_the_identity(tmp_path: Path) -> None:
    root = _write(tmp_path / "r", {**_FILES, ".env": "TOKEN=1", "node_modules/x/i.js": "1", "app/.venv/l.py": ""})
    first = capture_revision(root)
    (root / ".env").write_text("TOKEN=2")

    second = capture_revision(root)

    assert first.revision_id == second.revision_id
    assert {e.path for e in first.entries} == set(_FILES)
    assert first.stats.skipped_secret_files == 1


def test_symlinks_are_never_followed_and_nothing_outside_the_root_is_read(tmp_path: Path) -> None:
    outside = _write(tmp_path / "outside", {"secret.txt": "outside data", "dir/inner.py": "x"})
    root = _write(tmp_path / "r", _FILES)
    os.symlink(outside / "secret.txt", root / "app" / "linked.txt")
    os.symlink(outside / "dir", root / "app" / "linked_dir")
    os.symlink("../README.md", root / "app" / "internal_link.md")

    rev = capture_revision(root)

    assert {e.path for e in rev.entries} == set(_FILES)
    assert rev.stats.skipped_symlinks == 2  # the two file symlinks; the directory link is never entered


def test_fifos_and_other_special_files_are_skipped_without_blocking(tmp_path: Path) -> None:
    root = _write(tmp_path / "r", _FILES)
    os.mkfifo(root / "app" / "pipe")

    rev = capture_revision(root)

    assert {e.path for e in rev.entries} == set(_FILES)
    assert rev.stats.skipped_non_regular == 1


def test_budgets_fail_fast_with_a_typed_error(tmp_path: Path) -> None:
    root = _write(tmp_path / "r", _FILES)
    with pytest.raises(RevisionBudgetExceeded, match="max_files"):
        capture_revision(root, RevisionScope(max_files=2))
    with pytest.raises(RevisionBudgetExceeded, match="max_total_bytes"):
        capture_revision(root, RevisionScope(max_total_bytes=10))


def test_root_must_be_an_existing_directory(tmp_path: Path) -> None:
    with pytest.raises(RevisionCaptureError):
        capture_revision(tmp_path / "missing")
    (tmp_path / "file").write_text("x")
    with pytest.raises(RevisionCaptureError):
        capture_revision(tmp_path / "file")


# --- git work trees -------------------------------------------------------------------


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = _write(tmp_path / "repo", {**_FILES, ".gitignore": "*.log\n"})
    init_git_repo(root)
    commit_all(root, "initial")
    return root


def test_clean_git_tree_records_the_commit_as_provenance(repo: Path) -> None:
    rev = capture_revision(repo)
    head = run_git(["-C", str(repo), "rev-parse", "HEAD"]).strip()

    assert rev.kind is RevisionKind.GIT
    assert rev.commit_sha == head
    assert rev.dirty is False
    assert rev.changes is not None and (rev.changes.modified, rev.changes.added, rev.changes.deleted) == (0, 0, 0)


def test_git_and_plain_directory_with_identical_content_share_the_identity(repo: Path, tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    shutil.copytree(repo, plain, ignore=shutil.ignore_patterns(".git"))
    assert capture_revision(plain).revision_id == capture_revision(repo).revision_id


def test_dirty_untracked_ignored_and_deleted_files(repo: Path) -> None:
    clean = capture_revision(repo)
    (repo / "debug.log").write_text("ignored")  # .gitignore'd: invisible
    assert capture_revision(repo).revision_id == clean.revision_id

    (repo / "app/db.py").write_text("x = 2\n")
    (repo / "app/new.py").write_text("y = 1\n")
    (repo / "README.md").unlink()
    dirty = capture_revision(repo)

    assert dirty.dirty is True and dirty.revision_id != clean.revision_id
    assert dirty.changes is not None
    assert (dirty.changes.modified, dirty.changes.added, dirty.changes.deleted) == (1, 1, 1)
    assert "app/new.py" in {e.path for e in dirty.entries}

    tracked_only = capture_revision(repo, RevisionScope(include_untracked=False))
    assert "app/new.py" not in {e.path for e in tracked_only.entries}


def test_committing_identical_bytes_keeps_the_identity(repo: Path) -> None:
    (repo / "app/new.py").write_text("y = 1\n")
    dirty = capture_revision(repo)
    commit_all(repo, "add new.py")

    committed = capture_revision(repo)

    assert committed.dirty is False
    assert committed.commit_sha != dirty.commit_sha
    assert committed.revision_id == dirty.revision_id  # commit SHA is provenance, not identity


def test_unborn_repository_and_subdirectory_roots(tmp_path: Path, repo: Path) -> None:
    unborn = _write(tmp_path / "unborn", _FILES)
    init_git_repo(unborn)
    rev = capture_revision(unborn)
    assert rev.commit_sha is None and rev.dirty is True
    assert rev.changes is not None and rev.changes.added == 3

    sub = capture_revision(repo / "app")
    assert [e.path for e in sub.entries] == ["db.py", "main.py"]
    assert sub.dirty is False


def test_repository_configured_commands_are_never_executed(repo: Path, tmp_path: Path) -> None:
    marker = tmp_path / "EXECUTED"
    hook = f"sh -c 'touch {marker}'"
    run_git(["-C", str(repo), "config", "core.fsmonitor", hook])
    run_git(["-C", str(repo), "config", "filter.evil.clean", f"sh -c 'touch {marker}; cat'"])
    (repo / ".gitattributes").write_text("* filter=evil\n")
    (repo / "app/db.py").write_text("x = 3\n")

    rev = capture_revision(repo)

    assert rev.dirty is True
    assert not marker.exists(), "capture ran a repository-configured command"


# --- consistency --------------------------------------------------------------------------


def test_a_change_during_capture_is_retried_then_succeeds(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _write(tmp_path / "r", _FILES)
    original = revision_module._hash_file
    calls = {"n": 0}

    def hash_and_disturb(*args: object, **kwargs: object) -> object:
        result = original(*args, **kwargs)  # type: ignore[arg-type]
        calls["n"] += 1
        if calls["n"] == 2:  # first attempt only: rewrite an already-hashed file
            (root / "README.md").write_text("# changed during capture\n")
        return result

    monkeypatch.setattr(revision_module, "_hash_file", hash_and_disturb)
    rev = capture_revision(root)

    assert rev.stats.capture_attempts == 2
    monkeypatch.setattr(revision_module, "_hash_file", original)
    assert rev.revision_id == capture_revision(root).revision_id  # reflects the final, stable state


def test_a_tree_that_keeps_changing_raises_instead_of_returning_a_wrong_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _write(tmp_path / "r", _FILES)
    original = revision_module._hash_file
    counter = {"n": 0}

    def always_disturb(*args: object, **kwargs: object) -> object:
        result = original(*args, **kwargs)  # type: ignore[arg-type]
        counter["n"] += 1
        (root / "README.md").write_text("#" * counter["n"])
        return result

    monkeypatch.setattr(revision_module, "_hash_file", always_disturb)
    with pytest.raises(RevisionUnstableError):
        capture_revision(root)


def test_large_file_count_snapshot_is_complete_and_deterministic(tmp_path: Path) -> None:
    root = tmp_path / "many"
    for i in range(3000):
        _write(root, {f"pkg{i % 30}/m{i}.py": f"value = {i}\n"})

    first, second = capture_revision(root), capture_revision(root)

    assert first.file_count == 3000 and first.revision_id == second.revision_id
    assert first.entries == second.entries
