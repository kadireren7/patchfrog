"""Unit coverage for :mod:`patchfrog.review_memory.fingerprint` -- the
exact vs. semantic-family fingerprint split (see the module docstring)."""

from __future__ import annotations

import uuid

from patchfrog.analysis.domain import FindingCategory
from patchfrog.review_memory.fingerprint import (
    compute_exact_fingerprint,
    compute_semantic_family_fingerprint,
)

_REPO = uuid.uuid4()
_PR = uuid.uuid4()


def test_exact_fingerprint_deterministic() -> None:
    fp1 = compute_exact_fingerprint(
        repository_id=_REPO, pull_request_id=_PR, commit_sha="abc123", file_path="a.py",
        start_line=1, end_line=2, category=FindingCategory.CORRECTNESS, message="Bug here.",
    )
    fp2 = compute_exact_fingerprint(
        repository_id=_REPO, pull_request_id=_PR, commit_sha="abc123", file_path="a.py",
        start_line=1, end_line=2, category=FindingCategory.CORRECTNESS, message="Bug here.",
    )
    assert fp1 == fp2


def test_exact_fingerprint_changes_with_commit_sha() -> None:
    fp1 = compute_exact_fingerprint(
        repository_id=_REPO, pull_request_id=_PR, commit_sha="sha1", file_path="a.py",
        start_line=1, end_line=2, category=FindingCategory.CORRECTNESS, message="Bug here.",
    )
    fp2 = compute_exact_fingerprint(
        repository_id=_REPO, pull_request_id=_PR, commit_sha="sha2", file_path="a.py",
        start_line=1, end_line=2, category=FindingCategory.CORRECTNESS, message="Bug here.",
    )
    assert fp1 != fp2


def test_exact_fingerprint_changes_with_line_numbers() -> None:
    fp1 = compute_exact_fingerprint(
        repository_id=_REPO, pull_request_id=_PR, commit_sha="sha1", file_path="a.py",
        start_line=1, end_line=2, category=FindingCategory.CORRECTNESS, message="Bug here.",
    )
    fp2 = compute_exact_fingerprint(
        repository_id=_REPO, pull_request_id=_PR, commit_sha="sha1", file_path="a.py",
        start_line=10, end_line=11, category=FindingCategory.CORRECTNESS, message="Bug here.",
    )
    assert fp1 != fp2


def test_exact_fingerprint_message_normalization_ignores_whitespace_and_case() -> None:
    fp1 = compute_exact_fingerprint(
        repository_id=_REPO, pull_request_id=_PR, commit_sha="sha1", file_path="a.py",
        start_line=1, end_line=2, category=FindingCategory.CORRECTNESS, message="Bug   here.",
    )
    fp2 = compute_exact_fingerprint(
        repository_id=_REPO, pull_request_id=_PR, commit_sha="sha1", file_path="a.py",
        start_line=1, end_line=2, category=FindingCategory.CORRECTNESS, message="bug here.",
    )
    assert fp1 == fp2


def test_semantic_family_fingerprint_stable_across_line_movement_and_commit() -> None:
    """Deliberately takes no ``commit_sha``/line-number parameters at
    all -- calling it twice with identical logical identity always
    produces the same value regardless of commit or current line, which
    is what makes it usable as a cross-commit finding identity (see the
    module docstring)."""

    fp1 = compute_semantic_family_fingerprint(
        repository_id=_REPO, pull_request_id=_PR, file_path="a.py", symbol_qualified_name="Foo.bar",
        category=FindingCategory.CORRECTNESS, title="Off-by-one error",
    )
    fp2 = compute_semantic_family_fingerprint(
        repository_id=_REPO, pull_request_id=_PR, file_path="a.py", symbol_qualified_name="Foo.bar",
        category=FindingCategory.CORRECTNESS, title="Off-by-one error",
    )
    assert fp1 == fp2


def test_semantic_family_fingerprint_differs_by_symbol() -> None:
    fp1 = compute_semantic_family_fingerprint(
        repository_id=_REPO, pull_request_id=_PR, file_path="a.py", symbol_qualified_name="Foo.bar",
        category=FindingCategory.CORRECTNESS, title="Off-by-one error",
    )
    fp2 = compute_semantic_family_fingerprint(
        repository_id=_REPO, pull_request_id=_PR, file_path="a.py", symbol_qualified_name="Foo.baz",
        category=FindingCategory.CORRECTNESS, title="Off-by-one error",
    )
    assert fp1 != fp2


def test_semantic_family_fingerprint_module_level_uses_file_path_key() -> None:
    fp_a = compute_semantic_family_fingerprint(
        repository_id=_REPO, pull_request_id=_PR, file_path="a.py", symbol_qualified_name=None,
        category=FindingCategory.STYLE, title="Missing docstring",
    )
    fp_b = compute_semantic_family_fingerprint(
        repository_id=_REPO, pull_request_id=_PR, file_path="b.py", symbol_qualified_name=None,
        category=FindingCategory.STYLE, title="Missing docstring",
    )
    assert fp_a != fp_b


def test_fingerprints_never_cross_pull_requests() -> None:
    other_pr = uuid.uuid4()
    fp1 = compute_semantic_family_fingerprint(
        repository_id=_REPO, pull_request_id=_PR, file_path="a.py", symbol_qualified_name="foo",
        category=FindingCategory.CORRECTNESS, title="t",
    )
    fp2 = compute_semantic_family_fingerprint(
        repository_id=_REPO, pull_request_id=other_pr, file_path="a.py", symbol_qualified_name="foo",
        category=FindingCategory.CORRECTNESS, title="t",
    )
    assert fp1 != fp2


def test_fingerprints_never_cross_repositories() -> None:
    other_repo = uuid.uuid4()
    fp1 = compute_exact_fingerprint(
        repository_id=_REPO, pull_request_id=_PR, commit_sha="s", file_path="a.py",
        start_line=1, end_line=1, category=FindingCategory.CORRECTNESS, message="m",
    )
    fp2 = compute_exact_fingerprint(
        repository_id=other_repo, pull_request_id=_PR, commit_sha="s", file_path="a.py",
        start_line=1, end_line=1, category=FindingCategory.CORRECTNESS, message="m",
    )
    assert fp1 != fp2
