"""Top-level Executable Verification orchestrator.

:func:`build_executable_verification_report` is the one entry point,
called at most once per candidate, only after a real specialist
proposal exists for it (see
``validation/executable_verification/latest-summary.md`` section 7 for
why this integration point, not "before reviewer reasoning"). Reuses
:class:`~patchfrog.repository.snapshot.RepositorySnapshotProvider`
exactly like :class:`patchfrog.context.service.ContextService` already
does per candidate -- a fresh, exclusive, disposable snapshot per
attempt, never shared or mutated with anything else in the same review
run.
"""

from __future__ import annotations

import asyncio
import shutil
import tempfile
from pathlib import Path

from patchfrog.change_intelligence.domain import ExpectedCompanionChange
from patchfrog.executable_verification.domain import (
    EXECUTABLE_VERIFICATION_VERSION,
    MAX_TOTAL_VERIFICATION_SECONDS,
    MAX_VERIFICATION_SECONDS,
    MAX_VERIFICATIONS_PER_REVIEW,
    ExecutableVerificationEvidence,
    ExecutableVerificationReport,
    VerificationKind,
    VerificationOutcome,
)
from patchfrog.executable_verification.eligibility import determine_verification_target
from patchfrog.executable_verification.pytest_adapter import run_pytest_verification
from patchfrog.executable_verification.sandbox import VerificationSandbox, is_sandbox_available
from patchfrog.repository.git import GitError
from patchfrog.repository.snapshot import RepositorySnapshotProvider
from patchfrog.review.domain import ReviewCandidate

EMPTY_REPORT = ExecutableVerificationReport(version=EXECUTABLE_VERIFICATION_VERSION, attempted=False)


class VerificationBudget:
    """Shared, review-run-wide execution budget -- mirrors the existing
    ``budget_lock``/``budget_state`` token-budget pattern in
    :mod:`patchfrog.review.service`. Thread-/task-safe: multiple
    candidates may attempt verification concurrently."""

    def __init__(
        self,
        *,
        max_count: int = MAX_VERIFICATIONS_PER_REVIEW,
        max_total_seconds: float = MAX_TOTAL_VERIFICATION_SECONDS,
    ) -> None:
        self._lock = asyncio.Lock()
        self._count = 0
        self._total_seconds = 0.0
        self._max_count = max_count
        self._max_total_seconds = max_total_seconds

    async def try_reserve(self) -> bool:
        async with self._lock:
            if self._count >= self._max_count or self._total_seconds >= self._max_total_seconds:
                return False
            self._count += 1
            return True

    async def record_duration(self, duration_ms: float) -> None:
        async with self._lock:
            self._total_seconds += duration_ms / 1000.0


def sandbox_error_evidence(*, test_target_path: str, commit_sha: str) -> ExecutableVerificationEvidence:
    return ExecutableVerificationEvidence(
        kind=VerificationKind.EXISTING_TARGETED_TEST, outcome=VerificationOutcome.SANDBOX_ERROR,
        test_target_path=test_target_path, commit_sha=commit_sha, exit_code=None,
        stdout_excerpt="", stderr_excerpt="", duration_ms=0.0, timed_out=False,
    )


async def execute_against_snapshot(
    sandbox: VerificationSandbox, *, root_path: Path, test_target_path: str, commit_sha: str
) -> ExecutableVerificationEvidence:
    """Copy ``root_path`` into a fresh, disposable workspace and run the
    already-determined ``test_target_path`` there -- the original
    ``root_path`` is never mutated or deleted. Shared by
    :func:`build_executable_verification_report`'s own ``local=True``
    path (CLI/local review, single trust domain) and, since Milestone S6,
    the separate verifier process's own task
    (:mod:`apps.verifier.tasks`), which reuses this exact function
    against a review-worker-staged snapshot rather than re-implementing
    the same copy-then-sandbox sequence a second time."""

    workspace = Path(tempfile.mkdtemp(prefix="patchfrog-verify-"))
    try:
        shutil.copytree(root_path, workspace, dirs_exist_ok=True)
        return await run_pytest_verification(
            sandbox, workspace_root=workspace, test_target_path=test_target_path, commit_sha=commit_sha,
        )
    finally:
        shutil.rmtree(workspace, ignore_errors=True)


async def build_executable_verification_report(
    *,
    candidate: ReviewCandidate,
    expected_companions: tuple[ExpectedCompanionChange, ...],
    commit_sha: str,
    local: bool,
    budget: VerificationBudget,
    root_path: Path | None = None,
    clone_url: str | None = None,
    token: str | None = None,
    repository_full_name: str = "",
    snapshot_provider: RepositorySnapshotProvider | None = None,
) -> ExecutableVerificationReport:
    """``local=True`` (CLI/local review): ``root_path`` must be given --
    the existing on-disk checkout is copied into a fresh, disposable
    workspace before anything runs there, so the user's own working tree
    is never mutated (spec section E16). ``local=False`` (production):
    ``clone_url``/``token`` must be given -- reuses
    :class:`RepositorySnapshotProvider` for a fresh, exclusive clone,
    cleaned up via its own context manager."""

    target = determine_verification_target(candidate=candidate, expected_companions=expected_companions)
    if target is None:
        return EMPTY_REPORT

    if not is_sandbox_available():
        return ExecutableVerificationReport(
            version=EXECUTABLE_VERIFICATION_VERSION, attempted=True,
            evidence=sandbox_error_evidence(test_target_path=target, commit_sha=commit_sha),
        )

    if not await budget.try_reserve():
        return EMPTY_REPORT

    sandbox = VerificationSandbox(timeout_seconds=MAX_VERIFICATION_SECONDS)
    evidence: ExecutableVerificationEvidence
    try:
        if local:
            if root_path is None:
                raise ValueError("root_path is required when local=True")
            evidence = await execute_against_snapshot(
                sandbox, root_path=root_path, test_target_path=target, commit_sha=commit_sha,
            )
        else:
            if clone_url is None:
                raise ValueError("clone_url is required when local=False")
            provider = snapshot_provider or RepositorySnapshotProvider()
            with provider.acquire(
                clone_url=clone_url, commit_sha=commit_sha, repository_full_name=repository_full_name, token=token,
            ) as snapshot:
                evidence = await run_pytest_verification(
                    sandbox, workspace_root=snapshot.root_path, test_target_path=target, commit_sha=commit_sha,
                )
    except (GitError, OSError, ValueError):
        evidence = sandbox_error_evidence(test_target_path=target, commit_sha=commit_sha)

    await budget.record_duration(evidence.duration_ms)
    return ExecutableVerificationReport(version=EXECUTABLE_VERIFICATION_VERSION, attempted=True, evidence=evidence)
