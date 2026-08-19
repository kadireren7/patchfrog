"""Unit coverage for patchfrog.publishing.fingerprint -- stable,
content-addressed, never derived from a database auto-increment id."""

from __future__ import annotations

import uuid

from patchfrog.analysis.domain import Confidence, FindingCategory, Severity
from patchfrog.publishing.domain import PublishableFinding
from patchfrog.publishing.fingerprint import compute_finding_fingerprint

_REPO_ID = uuid.uuid4()


def _finding(**overrides: object) -> PublishableFinding:
    defaults: dict[str, object] = {
        "finding_id": uuid.uuid4(),
        "title": "t",
        "message": "m",
        "category": FindingCategory.CORRECTNESS,
        "severity": Severity.MEDIUM,
        "confidence": Confidence.MEDIUM,
        "file_path": "src/x.py",
        "start_line": 1,
        "end_line": 1,
        "reasoning_summary": "r",
        "suggested_fix": None,
    }
    defaults.update(overrides)
    return PublishableFinding(**defaults)  # type: ignore[arg-type]


def test_fingerprint_independent_of_finding_id() -> None:
    f1 = _finding(finding_id=uuid.uuid4())
    f2 = _finding(finding_id=uuid.uuid4())
    fp1 = compute_finding_fingerprint(repository_id=_REPO_ID, pull_request_number=1, head_sha="a" * 40, finding=f1)
    fp2 = compute_finding_fingerprint(repository_id=_REPO_ID, pull_request_number=1, head_sha="a" * 40, finding=f2)
    assert fp1 == fp2


def test_fingerprint_changes_with_head_sha() -> None:
    f = _finding()
    fp1 = compute_finding_fingerprint(repository_id=_REPO_ID, pull_request_number=1, head_sha="a" * 40, finding=f)
    fp2 = compute_finding_fingerprint(repository_id=_REPO_ID, pull_request_number=1, head_sha="b" * 40, finding=f)
    assert fp1 != fp2


def test_fingerprint_changes_with_location() -> None:
    f1 = _finding(start_line=1, end_line=1)
    f2 = _finding(start_line=2, end_line=2)
    fp1 = compute_finding_fingerprint(repository_id=_REPO_ID, pull_request_number=1, head_sha="a" * 40, finding=f1)
    fp2 = compute_finding_fingerprint(repository_id=_REPO_ID, pull_request_number=1, head_sha="a" * 40, finding=f2)
    assert fp1 != fp2


def test_fingerprint_message_whitespace_normalized() -> None:
    f1 = _finding(message="hello   world")
    f2 = _finding(message="Hello World")  # different case AND whitespace
    fp1 = compute_finding_fingerprint(repository_id=_REPO_ID, pull_request_number=1, head_sha="a" * 40, finding=f1)
    fp2 = compute_finding_fingerprint(repository_id=_REPO_ID, pull_request_number=1, head_sha="a" * 40, finding=f2)
    assert fp1 == fp2


def test_fingerprint_changes_with_pull_request_number() -> None:
    f = _finding()
    fp1 = compute_finding_fingerprint(repository_id=_REPO_ID, pull_request_number=1, head_sha="a" * 40, finding=f)
    fp2 = compute_finding_fingerprint(repository_id=_REPO_ID, pull_request_number=2, head_sha="a" * 40, finding=f)
    assert fp1 != fp2
