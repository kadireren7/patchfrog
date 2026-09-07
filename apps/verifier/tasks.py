"""The one task this process runs: execute one bounded, already-eligible
verification request against an already-staged, integrity-checked
repository snapshot.

Reuses :func:`patchfrog.executable_verification.service.execute_against_snapshot`
-- the exact same "copy into a disposable workspace, then run the
hardened bwrap sandbox" sequence Milestone S's own ``local=True`` (CLI)
path already uses, completely unmodified. This task adds nothing to the
sandbox itself; it only decides *whether* to call it (snapshot integrity
first) and translates between the wire protocol and the existing
in-process types.

Eligibility (which test file to run) is **not** re-derived here -- the
review worker already determined ``test_target_path`` authoritatively
before ever enqueueing a request (see
``validation/production_execution/latest-summary.md`` Part F/G); this
task trusts that decision and never re-runs
:func:`patchfrog.executable_verification.eligibility.determine_verification_target`
itself. A verifier that re-derived eligibility from reconstructed,
partial candidate data would be trusting attacker-adjacent input for a
decision the review worker (which has the real, complete data) already
made correctly.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from apps.verifier.celery_app import verifier_celery_app
from patchfrog.executable_verification.domain import (
    MAX_VERIFICATION_SECONDS,
    VerificationKind,
    VerificationOutcome,
)
from patchfrog.executable_verification.protocol import (
    VERIFICATION_TASK_NAME,
    VERIFIER_PROTOCOL_VERSION,
    VerificationExecutionRequest,
    VerificationExecutionResult,
)
from patchfrog.executable_verification.sandbox import VerificationSandbox, is_sandbox_available
from patchfrog.executable_verification.service import execute_against_snapshot
from patchfrog.executable_verification.snapshot_staging import verify_snapshot_integrity


def _error_result(request: VerificationExecutionRequest, *, outcome: VerificationOutcome) -> dict[str, object]:
    return VerificationExecutionResult(
        request_id=request.request_id,
        protocol_version=VERIFIER_PROTOCOL_VERSION,
        commit_sha=request.commit_sha,
        verification_kind=request.verification_kind,
        test_target_path=request.test_target_path,
        outcome=outcome,
        exit_code=None,
        stdout_excerpt="",
        stderr_excerpt="",
        duration_ms=0.0,
        timed_out=False,
    ).to_wire()


async def _run_verification(request: VerificationExecutionRequest) -> dict[str, object]:
    if request.verification_kind is not VerificationKind.EXISTING_TARGETED_TEST:
        # v1 implements exactly one kind -- see domain.py's own docstring.
        # Never guessed at or silently substituted.
        return _error_result(request, outcome=VerificationOutcome.UNSUPPORTED)

    snapshot_path = Path(request.snapshot_path)
    if not verify_snapshot_integrity(root_path=snapshot_path, expected_commit_sha=request.commit_sha):
        return _error_result(request, outcome=VerificationOutcome.SANDBOX_ERROR)

    if not is_sandbox_available():
        return _error_result(request, outcome=VerificationOutcome.SANDBOX_ERROR)

    timeout_seconds = min(request.timeout_seconds, MAX_VERIFICATION_SECONDS)
    sandbox = VerificationSandbox(timeout_seconds=timeout_seconds)
    evidence = await execute_against_snapshot(
        sandbox, root_path=snapshot_path, test_target_path=request.test_target_path, commit_sha=request.commit_sha,
    )

    return VerificationExecutionResult(
        request_id=request.request_id,
        protocol_version=VERIFIER_PROTOCOL_VERSION,
        commit_sha=evidence.commit_sha,
        verification_kind=evidence.kind,
        test_target_path=evidence.test_target_path,
        outcome=evidence.outcome,
        exit_code=evidence.exit_code,
        stdout_excerpt=evidence.stdout_excerpt,
        stderr_excerpt=evidence.stderr_excerpt,
        duration_ms=evidence.duration_ms,
        timed_out=evidence.timed_out,
    ).to_wire()


@verifier_celery_app.task(name=VERIFICATION_TASK_NAME)  # type: ignore[untyped-decorator]
def verify_candidate_task(request_data: dict[str, object]) -> dict[str, object]:
    request = VerificationExecutionRequest.from_wire(request_data)
    return asyncio.run(_run_verification(request))
