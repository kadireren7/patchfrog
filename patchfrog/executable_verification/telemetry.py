"""Compact, persistence-ready summary across every
:class:`~patchfrog.executable_verification.domain.ExecutableVerificationReport`
produced in one review run -- counts only. No test target path, no
stdout/stderr excerpt, no commit SHA, no candidate identity anywhere in
telemetry."""

from __future__ import annotations

from dataclasses import dataclass

from patchfrog.executable_verification.domain import (
    ExecutableVerificationReport,
    VerificationOutcome,
)


@dataclass(frozen=True, slots=True)
class ExecutableVerificationSummary:
    version: int
    executable_verification_attempted_count: int
    executable_verification_confirmed_failure_count: int
    executable_verification_passed_count: int
    executable_verification_timeout_count: int
    executable_verification_unsupported_count: int
    executable_verification_inconclusive_count: int


def summarize_for_persistence(
    reports: tuple[ExecutableVerificationReport, ...], *, version: int
) -> ExecutableVerificationSummary:
    attempted = [r for r in reports if r.attempted and r.evidence is not None]
    counts = dict.fromkeys(VerificationOutcome, 0)
    for report in attempted:
        assert report.evidence is not None
        counts[report.evidence.outcome] += 1

    return ExecutableVerificationSummary(
        version=version,
        executable_verification_attempted_count=len(attempted),
        executable_verification_confirmed_failure_count=counts[VerificationOutcome.CONFIRMED_FAILURE],
        executable_verification_passed_count=counts[VerificationOutcome.PASSED],
        executable_verification_timeout_count=counts[VerificationOutcome.TIMEOUT],
        executable_verification_unsupported_count=counts[VerificationOutcome.UNSUPPORTED],
        executable_verification_inconclusive_count=counts[VerificationOutcome.INCONCLUSIVE]
        + counts[VerificationOutcome.SANDBOX_ERROR],
    )
