from __future__ import annotations

import uuid

from patchfrog.agent_handoff.domain import (
    MAX_HANDOFF_TEXT_BYTES,
    bound_text,
    compute_handoff_id,
)


def test_compute_handoff_id_deterministic() -> None:
    repository_id = uuid.uuid4()
    finding_id = uuid.uuid4()
    review_run_id = uuid.uuid4()
    first = compute_handoff_id(
        repository_id=repository_id, finding_id=finding_id, review_run_id=review_run_id,
        original_commit_sha="a" * 40,
    )
    second = compute_handoff_id(
        repository_id=repository_id, finding_id=finding_id, review_run_id=review_run_id,
        original_commit_sha="a" * 40,
    )
    assert first == second


def test_compute_handoff_id_differs_on_different_finding() -> None:
    repository_id = uuid.uuid4()
    review_run_id = uuid.uuid4()
    a = compute_handoff_id(
        repository_id=repository_id, finding_id=uuid.uuid4(), review_run_id=review_run_id,
        original_commit_sha="a" * 40,
    )
    b = compute_handoff_id(
        repository_id=repository_id, finding_id=uuid.uuid4(), review_run_id=review_run_id,
        original_commit_sha="a" * 40,
    )
    assert a != b


def test_compute_handoff_id_differs_on_different_commit() -> None:
    repository_id = uuid.uuid4()
    finding_id = uuid.uuid4()
    review_run_id = uuid.uuid4()
    a = compute_handoff_id(
        repository_id=repository_id, finding_id=finding_id, review_run_id=review_run_id,
        original_commit_sha="a" * 40,
    )
    b = compute_handoff_id(
        repository_id=repository_id, finding_id=finding_id, review_run_id=review_run_id,
        original_commit_sha="b" * 40,
    )
    assert a != b


def test_compute_handoff_id_differs_on_different_review_run() -> None:
    repository_id = uuid.uuid4()
    finding_id = uuid.uuid4()
    a = compute_handoff_id(
        repository_id=repository_id, finding_id=finding_id, review_run_id=uuid.uuid4(),
        original_commit_sha="a" * 40,
    )
    b = compute_handoff_id(
        repository_id=repository_id, finding_id=finding_id, review_run_id=uuid.uuid4(),
        original_commit_sha="a" * 40,
    )
    assert a != b


def test_bound_text_leaves_short_text_unchanged() -> None:
    assert bound_text("short") == "short"


def test_bound_text_truncates_oversized_text() -> None:
    text = "x" * (MAX_HANDOFF_TEXT_BYTES * 2)
    result = bound_text(text)
    assert len(result.encode("utf-8")) <= MAX_HANDOFF_TEXT_BYTES + len("...(truncated)")
    assert result.endswith("...(truncated)")
