"""Unit tests for :mod:`patchfrog.executable_verification.telemetry` --
counts only, no test target path, no stdout/stderr excerpt, no commit
SHA, no candidate identity anywhere."""

from __future__ import annotations

from patchfrog.executable_verification.domain import (
    EXECUTABLE_VERIFICATION_VERSION,
    ExecutableVerificationEvidence,
    ExecutableVerificationReport,
    VerificationKind,
    VerificationOutcome,
)
from patchfrog.executable_verification.telemetry import summarize_for_persistence


def _report(outcome: VerificationOutcome | None) -> ExecutableVerificationReport:
    if outcome is None:
        return ExecutableVerificationReport(version=EXECUTABLE_VERIFICATION_VERSION, attempted=False)
    evidence = ExecutableVerificationEvidence(
        kind=VerificationKind.EXISTING_TARGETED_TEST, outcome=outcome, test_target_path="tests/test_x.py",
        commit_sha="a" * 40, exit_code=0, stdout_excerpt="", stderr_excerpt="", duration_ms=1.0, timed_out=False,
    )
    return ExecutableVerificationReport(version=EXECUTABLE_VERIFICATION_VERSION, attempted=True, evidence=evidence)


def test_empty_reports_all_zero() -> None:
    summary = summarize_for_persistence((), version=EXECUTABLE_VERIFICATION_VERSION)
    assert summary.executable_verification_attempted_count == 0
    assert summary.executable_verification_confirmed_failure_count == 0
    assert summary.executable_verification_passed_count == 0
    assert summary.executable_verification_timeout_count == 0
    assert summary.executable_verification_unsupported_count == 0
    assert summary.executable_verification_inconclusive_count == 0


def test_not_attempted_reports_never_counted() -> None:
    reports = (_report(None), _report(None))
    summary = summarize_for_persistence(reports, version=EXECUTABLE_VERIFICATION_VERSION)
    assert summary.executable_verification_attempted_count == 0


def test_counts_by_outcome() -> None:
    reports = (
        _report(VerificationOutcome.CONFIRMED_FAILURE),
        _report(VerificationOutcome.PASSED),
        _report(VerificationOutcome.PASSED),
        _report(VerificationOutcome.TIMEOUT),
        _report(VerificationOutcome.UNSUPPORTED),
        _report(VerificationOutcome.INCONCLUSIVE),
        _report(VerificationOutcome.SANDBOX_ERROR),
        _report(None),
    )
    summary = summarize_for_persistence(reports, version=EXECUTABLE_VERIFICATION_VERSION)
    assert summary.executable_verification_attempted_count == 7
    assert summary.executable_verification_confirmed_failure_count == 1
    assert summary.executable_verification_passed_count == 2
    assert summary.executable_verification_timeout_count == 1
    assert summary.executable_verification_unsupported_count == 1
    # SANDBOX_ERROR is folded into inconclusive -- no separate persisted column.
    assert summary.executable_verification_inconclusive_count == 2


def test_no_identity_or_text_fields_on_summary() -> None:
    from dataclasses import fields

    from patchfrog.executable_verification.telemetry import ExecutableVerificationSummary

    field_names = {f.name for f in fields(ExecutableVerificationSummary)}
    assert not any(name.endswith("_text") or name.endswith("_rendered") for name in field_names)
    assert not any(
        "path" in name or "commit" in name or "candidate" in name or "excerpt" in name for name in field_names
    )
