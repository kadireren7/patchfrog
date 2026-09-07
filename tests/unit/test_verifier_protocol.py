"""Unit tests for the Milestone S6 verifier wire protocol -- request/result
serialization round-trip, deterministic request identity, and result
authenticity matching."""

from __future__ import annotations

from patchfrog.executable_verification.domain import VerificationKind, VerificationOutcome
from patchfrog.executable_verification.protocol import (
    VERIFIER_PROTOCOL_VERSION,
    VerificationExecutionRequest,
    VerificationExecutionResult,
    compute_request_id,
    result_matches_request,
)


def _request(**overrides: object) -> VerificationExecutionRequest:
    defaults: dict[str, object] = {
        "request_id": "req-1",
        "protocol_version": VERIFIER_PROTOCOL_VERSION,
        "repository_id": "repo-1",
        "review_run_id": "run-1",
        "commit_sha": "a" * 40,
        "verification_kind": VerificationKind.EXISTING_TARGETED_TEST,
        "test_target_path": "tests/test_capture.py",
        "snapshot_path": "/shared/snap-1",
        "timeout_seconds": 30.0,
    }
    defaults.update(overrides)
    return VerificationExecutionRequest(**defaults)  # type: ignore[arg-type]


def _result(**overrides: object) -> VerificationExecutionResult:
    defaults: dict[str, object] = {
        "request_id": "req-1",
        "protocol_version": VERIFIER_PROTOCOL_VERSION,
        "commit_sha": "a" * 40,
        "verification_kind": VerificationKind.EXISTING_TARGETED_TEST,
        "test_target_path": "tests/test_capture.py",
        "outcome": VerificationOutcome.PASSED,
        "exit_code": 0,
        "stdout_excerpt": "ok",
        "stderr_excerpt": "",
        "duration_ms": 12.5,
        "timed_out": False,
    }
    defaults.update(overrides)
    return VerificationExecutionResult(**defaults)  # type: ignore[arg-type]


def test_request_wire_round_trip() -> None:
    request = _request()
    restored = VerificationExecutionRequest.from_wire(request.to_wire())
    assert restored == request


def test_result_wire_round_trip() -> None:
    result = _result()
    restored = VerificationExecutionResult.from_wire(result.to_wire())
    assert restored == result


def test_result_wire_round_trip_with_none_exit_code() -> None:
    result = _result(exit_code=None, outcome=VerificationOutcome.SANDBOX_ERROR)
    restored = VerificationExecutionResult.from_wire(result.to_wire())
    assert restored.exit_code is None


def test_compute_request_id_deterministic() -> None:
    kwargs = {
        "repository_id": "repo-1", "review_run_id": "run-1", "commit_sha": "a" * 40,
        "file_path": "src/capture.py", "qualified_name": "capture_payment",
        "verification_kind": VerificationKind.EXISTING_TARGETED_TEST,
        "test_target_path": "tests/test_capture.py",
    }
    assert compute_request_id(**kwargs) == compute_request_id(**kwargs)  # type: ignore[arg-type]


def test_compute_request_id_differs_on_different_commit() -> None:
    base = {
        "repository_id": "repo-1", "review_run_id": "run-1",
        "file_path": "src/capture.py", "qualified_name": "capture_payment",
        "verification_kind": VerificationKind.EXISTING_TARGETED_TEST,
        "test_target_path": "tests/test_capture.py",
    }
    a = compute_request_id(commit_sha="a" * 40, **base)  # type: ignore[arg-type]
    b = compute_request_id(commit_sha="b" * 40, **base)  # type: ignore[arg-type]
    assert a != b


def test_compute_request_id_differs_on_different_review_run() -> None:
    base = {
        "repository_id": "repo-1", "commit_sha": "a" * 40,
        "file_path": "src/capture.py", "qualified_name": "capture_payment",
        "verification_kind": VerificationKind.EXISTING_TARGETED_TEST,
        "test_target_path": "tests/test_capture.py",
    }
    a = compute_request_id(review_run_id="run-1", **base)  # type: ignore[arg-type]
    b = compute_request_id(review_run_id="run-2", **base)  # type: ignore[arg-type]
    assert a != b


def test_compute_request_id_differs_on_different_candidate() -> None:
    base = {
        "repository_id": "repo-1", "review_run_id": "run-1", "commit_sha": "a" * 40,
        "verification_kind": VerificationKind.EXISTING_TARGETED_TEST,
        "test_target_path": "tests/test_capture.py",
    }
    a = compute_request_id(file_path="src/a.py", qualified_name="foo", **base)  # type: ignore[arg-type]
    b = compute_request_id(file_path="src/b.py", qualified_name="foo", **base)  # type: ignore[arg-type]
    assert a != b


def test_compute_request_id_handles_none_qualified_name() -> None:
    request_id = compute_request_id(
        repository_id="repo-1", review_run_id="run-1", commit_sha="a" * 40,
        file_path="src/module.py", qualified_name=None,
        verification_kind=VerificationKind.EXISTING_TARGETED_TEST,
        test_target_path="tests/test_module.py",
    )
    assert isinstance(request_id, str) and len(request_id) == 64


def test_result_matches_request_true_for_identical_identity() -> None:
    request = _request()
    result = _result()
    assert result_matches_request(request=request, result=result) is True


def test_result_matches_request_false_on_request_id_mismatch() -> None:
    request = _request()
    result = _result(request_id="different-request-id")
    assert result_matches_request(request=request, result=result) is False


def test_result_matches_request_false_on_commit_sha_mismatch() -> None:
    request = _request()
    result = _result(commit_sha="c" * 40)
    assert result_matches_request(request=request, result=result) is False


def test_result_matches_request_false_on_verification_kind_mismatch() -> None:
    request = _request()
    result = _result(verification_kind=VerificationKind.STATIC_REPRODUCTION)
    assert result_matches_request(request=request, result=result) is False


def test_result_matches_request_false_on_test_target_mismatch() -> None:
    request = _request()
    result = _result(test_target_path="tests/test_other.py")
    assert result_matches_request(request=request, result=result) is False
