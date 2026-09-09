"""Unit tests for the Milestone S6 verifier wire protocol -- request/result
serialization round-trip, deterministic request identity, protocol-version
enforcement, strict wire-type validation (no silent Python coercion of a
malformed payload), and result authenticity matching."""

from __future__ import annotations

import pytest

from patchfrog.executable_verification.domain import VerificationKind, VerificationOutcome
from patchfrog.executable_verification.protocol import (
    VERIFIER_PROTOCOL_VERSION,
    ProtocolValidationError,
    VerificationExecutionRequest,
    VerificationExecutionResult,
    compute_request_id,
    enforce_request_protocol_version,
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
        "artifact_id": "patchfrog-artifact-abc123",
        "artifact_digest": "d" * 64,
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


def test_result_matches_request_false_on_protocol_version_mismatch() -> None:
    request = _request()
    result = _result(protocol_version=VERIFIER_PROTOCOL_VERSION + 1)
    assert result_matches_request(request=request, result=result) is False


def test_result_matches_request_false_when_result_protocol_version_not_current() -> None:
    """Even if request and result happen to agree with each other, a
    result claiming a non-current protocol version is still rejected."""

    request = _request(protocol_version=999)
    result = _result(protocol_version=999)
    assert result_matches_request(request=request, result=result) is False


# ---- Protocol version enforcement ----


def test_enforce_request_protocol_version_accepts_current() -> None:
    assert enforce_request_protocol_version(_request(protocol_version=VERIFIER_PROTOCOL_VERSION)) is True


def test_enforce_request_protocol_version_rejects_zero() -> None:
    assert enforce_request_protocol_version(_request(protocol_version=0)) is False


def test_enforce_request_protocol_version_rejects_future_value() -> None:
    assert enforce_request_protocol_version(_request(protocol_version=VERIFIER_PROTOCOL_VERSION + 1)) is False


def test_enforce_request_protocol_version_rejects_negative() -> None:
    assert enforce_request_protocol_version(_request(protocol_version=-1)) is False


# ---- Strict wire parsing: no silent Python-coercion of malformed values ----


def test_request_from_wire_rejects_missing_field() -> None:
    data = _request().to_wire()
    del data["commit_sha"]
    with pytest.raises(ProtocolValidationError):
        VerificationExecutionRequest.from_wire(data)


def test_request_from_wire_rejects_non_string_commit_sha() -> None:
    data = _request().to_wire()
    data["commit_sha"] = 12345
    with pytest.raises(ProtocolValidationError):
        VerificationExecutionRequest.from_wire(data)


def test_request_from_wire_rejects_bool_as_protocol_version() -> None:
    """bool is a subclass of int in Python -- must never be silently
    accepted as an int field."""

    data = _request().to_wire()
    data["protocol_version"] = True
    with pytest.raises(ProtocolValidationError):
        VerificationExecutionRequest.from_wire(data)


def test_request_from_wire_rejects_unknown_verification_kind() -> None:
    data = _request().to_wire()
    data["verification_kind"] = "arbitrary_shell_command"
    with pytest.raises(ProtocolValidationError):
        VerificationExecutionRequest.from_wire(data)


def test_request_from_wire_rejects_out_of_bounds_timeout() -> None:
    data = _request().to_wire()
    data["timeout_seconds"] = 999999.0
    with pytest.raises(ProtocolValidationError):
        VerificationExecutionRequest.from_wire(data)


def test_request_from_wire_rejects_negative_timeout() -> None:
    data = _request().to_wire()
    data["timeout_seconds"] = -5.0
    with pytest.raises(ProtocolValidationError):
        VerificationExecutionRequest.from_wire(data)


def test_request_from_wire_rejects_nan_timeout() -> None:
    data = _request().to_wire()
    data["timeout_seconds"] = float("nan")
    with pytest.raises(ProtocolValidationError):
        VerificationExecutionRequest.from_wire(data)


def test_request_from_wire_rejects_empty_artifact_id() -> None:
    data = _request().to_wire()
    data["artifact_id"] = ""
    with pytest.raises(ProtocolValidationError):
        VerificationExecutionRequest.from_wire(data)


def test_result_from_wire_rejects_string_timed_out_true() -> None:
    """The exact trap the correction named: bool("false") == True. A
    string in the timed_out slot must be rejected outright, never
    coerced."""

    data = _result().to_wire()
    data["timed_out"] = "false"
    with pytest.raises(ProtocolValidationError):
        VerificationExecutionResult.from_wire(data)


def test_result_from_wire_rejects_string_timed_out_false() -> None:
    data = _result().to_wire()
    data["timed_out"] = "true"
    with pytest.raises(ProtocolValidationError):
        VerificationExecutionResult.from_wire(data)


def test_result_from_wire_rejects_non_int_exit_code() -> None:
    data = _result().to_wire()
    data["exit_code"] = "0"
    with pytest.raises(ProtocolValidationError):
        VerificationExecutionResult.from_wire(data)


def test_result_from_wire_rejects_bool_exit_code() -> None:
    data = _result().to_wire()
    data["exit_code"] = True
    with pytest.raises(ProtocolValidationError):
        VerificationExecutionResult.from_wire(data)


def test_result_from_wire_accepts_null_exit_code() -> None:
    data = _result().to_wire()
    data["exit_code"] = None
    result = VerificationExecutionResult.from_wire(data)
    assert result.exit_code is None


def test_result_from_wire_rejects_unknown_outcome() -> None:
    data = _result().to_wire()
    data["outcome"] = "totally_made_up_outcome"
    with pytest.raises(ProtocolValidationError):
        VerificationExecutionResult.from_wire(data)


def test_result_from_wire_rejects_negative_duration() -> None:
    data = _result().to_wire()
    data["duration_ms"] = -1.0
    with pytest.raises(ProtocolValidationError):
        VerificationExecutionResult.from_wire(data)


def test_result_from_wire_rejects_infinite_duration() -> None:
    data = _result().to_wire()
    data["duration_ms"] = float("inf")
    with pytest.raises(ProtocolValidationError):
        VerificationExecutionResult.from_wire(data)


def test_result_from_wire_rejects_oversized_stdout_excerpt() -> None:
    data = _result().to_wire()
    data["stdout_excerpt"] = "x" * 10_000_000
    with pytest.raises(ProtocolValidationError):
        VerificationExecutionResult.from_wire(data)


def test_result_from_wire_rejects_non_string_stdout_excerpt() -> None:
    data = _result().to_wire()
    data["stdout_excerpt"] = 12345
    with pytest.raises(ProtocolValidationError):
        VerificationExecutionResult.from_wire(data)
