from __future__ import annotations

import uuid

from patchfrog.context.domain import ContextTarget, ContextTargetType

_REPO_ID = uuid.uuid4()
_INDEX_ID = uuid.uuid4()
_SHA = "a" * 40


def _target(**overrides: object) -> ContextTarget:
    kwargs: dict[str, object] = {
        "repository_id": _REPO_ID,
        "repository_index_id": _INDEX_ID,
        "commit_sha": _SHA,
        "target_type": ContextTargetType.LINE,
        "file_path": "src/cache.py",
        "line": 42,
    }
    kwargs.update(overrides)
    return ContextTarget(**kwargs)  # type: ignore[arg-type]


def test_identical_targets_have_identical_fingerprints() -> None:
    assert _target().fingerprint() == _target().fingerprint()


def test_different_line_changes_fingerprint() -> None:
    assert _target(line=42).fingerprint() != _target(line=43).fingerprint()


def test_different_file_path_changes_fingerprint() -> None:
    assert _target(file_path="a.py").fingerprint() != _target(file_path="b.py").fingerprint()


def test_different_target_type_changes_fingerprint() -> None:
    line = _target(target_type=ContextTargetType.LINE, line=1, symbol_id=None)
    sym = _target(target_type=ContextTargetType.SYMBOL, line=None, symbol_id=uuid.uuid4())
    assert line.fingerprint() != sym.fingerprint()


def test_fingerprint_excludes_repository_and_commit() -> None:
    """repository_id/commit_sha are combined separately by the caller
    (see ContextService._run) -- the target fingerprint alone must be
    identical across repos/commits for the same logical target."""

    a = _target(repository_id=uuid.uuid4(), commit_sha="a" * 40)
    b = _target(repository_id=uuid.uuid4(), commit_sha="b" * 40)
    assert a.fingerprint() == b.fingerprint()


def test_finding_id_participates_in_fingerprint() -> None:
    a = _target(finding_id=uuid.uuid4())
    b = _target(finding_id=uuid.uuid4())
    assert a.fingerprint() != b.fingerprint()
