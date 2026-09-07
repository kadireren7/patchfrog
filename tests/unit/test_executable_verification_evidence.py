"""Unit tests for :mod:`patchfrog.executable_verification.evidence` --
bounded, critic-only evidence text. Never tells the LLM a conclusion;
only PASSED/CONFIRMED_FAILURE are ever rendered -- every other outcome
(TIMEOUT/SANDBOX_ERROR/UNSUPPORTED/INCONCLUSIVE) renders empty, since
none of them carry anything actionable."""

from __future__ import annotations

from patchfrog.executable_verification.domain import (
    EXECUTABLE_VERIFICATION_VERSION,
    ExecutableVerificationEvidence,
    ExecutableVerificationReport,
    VerificationKind,
    VerificationOutcome,
)
from patchfrog.executable_verification.evidence import evidence_text_for_report


def _evidence(outcome: VerificationOutcome, *, stdout: str = "") -> ExecutableVerificationEvidence:
    return ExecutableVerificationEvidence(
        kind=VerificationKind.EXISTING_TARGETED_TEST, outcome=outcome, test_target_path="tests/test_capture.py",
        commit_sha="a" * 40, exit_code=0, stdout_excerpt=stdout, stderr_excerpt="", duration_ms=10.0, timed_out=False,
    )


def test_not_attempted_report_empty() -> None:
    report = ExecutableVerificationReport(version=EXECUTABLE_VERIFICATION_VERSION, attempted=False)
    assert evidence_text_for_report(report) == ""


def test_passed_renders_neutral_text() -> None:
    report = ExecutableVerificationReport(
        version=EXECUTABLE_VERIFICATION_VERSION, attempted=True, evidence=_evidence(VerificationOutcome.PASSED),
    )
    text = evidence_text_for_report(report)
    assert "tests/test_capture.py" in text
    assert "passed" in text
    assert "conclusion" in text.lower()  # the neutral instruction is present
    assert "proves" not in text.lower()
    assert "wrong" not in text.lower()


def test_confirmed_failure_renders_excerpt() -> None:
    report = ExecutableVerificationReport(
        version=EXECUTABLE_VERIFICATION_VERSION, attempted=True,
        evidence=_evidence(VerificationOutcome.CONFIRMED_FAILURE, stdout="AssertionError: boom"),
    )
    text = evidence_text_for_report(report)
    assert "confirmed_failure" in text
    assert "AssertionError: boom" in text
    assert "will break" not in text.lower()


def test_timeout_renders_empty() -> None:
    report = ExecutableVerificationReport(
        version=EXECUTABLE_VERIFICATION_VERSION, attempted=True, evidence=_evidence(VerificationOutcome.TIMEOUT),
    )
    assert evidence_text_for_report(report) == ""


def test_sandbox_error_renders_empty() -> None:
    report = ExecutableVerificationReport(
        version=EXECUTABLE_VERIFICATION_VERSION, attempted=True,
        evidence=_evidence(VerificationOutcome.SANDBOX_ERROR),
    )
    assert evidence_text_for_report(report) == ""


def test_unsupported_renders_empty() -> None:
    report = ExecutableVerificationReport(
        version=EXECUTABLE_VERIFICATION_VERSION, attempted=True,
        evidence=_evidence(VerificationOutcome.UNSUPPORTED),
    )
    assert evidence_text_for_report(report) == ""


def test_inconclusive_renders_empty() -> None:
    report = ExecutableVerificationReport(
        version=EXECUTABLE_VERIFICATION_VERSION, attempted=True,
        evidence=_evidence(VerificationOutcome.INCONCLUSIVE),
    )
    assert evidence_text_for_report(report) == ""
