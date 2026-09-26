"""Best-effort static type check verification step (M8.4's ``TYPE_CHECK_PASSES``).

Uses PatchFrog's own bundled ``mypy`` (already a project dependency, used
by :mod:`patchfrog.analysis.analyzers` for review) against the target
repository's modified files. Never mandatory alone for VERIFIED (see
:mod:`patchfrog.migration_verification.requirements`) -- a target
repository's own type-checking configuration/stubs are not installed in
PatchFrog's sandbox, so this is evidence, never proof by itself."""

from __future__ import annotations

import sys
from pathlib import Path

from patchfrog.executable_verification.sandbox import (
    VerificationSandbox,
    stderr_excerpt,
    stdout_excerpt,
)
from patchfrog.migration_verification.domain import CheckStatus, StepEvidence, VerificationStepKind

_PYTHON = sys.executable
_EXIT_NO_ERRORS = 0
_EXIT_TYPE_ERRORS = 1
_EXIT_USAGE_ERROR = 2


async def run_type_check(
    sandbox: VerificationSandbox, *, workspace_root: Path, step_id: str, requirement_ids: tuple[str, ...],
    file_paths: tuple[str, ...],
) -> StepEvidence:
    if not file_paths:
        return StepEvidence(
            step_id=step_id, kind=VerificationStepKind.TYPE_CHECK, requirement_ids=requirement_ids,
            status=CheckStatus.NOT_RUN, exit_code=None,
            duration_ms=0.0, stdout_excerpt="", stderr_excerpt="", detail="no Python files to type-check",
        )
    result = await sandbox.run(
        [_PYTHON, "-m", "mypy", "--ignore-missing-imports", "--no-error-summary", *file_paths], cwd=workspace_root,
    )
    if result.timed_out:
        status = CheckStatus.UNAVAILABLE
        detail = "type check timed out"
    elif result.exit_code == _EXIT_NO_ERRORS:
        status = CheckStatus.PASSED
        detail = f"{len(file_paths)} file(s) type-checked cleanly"
    elif result.exit_code == _EXIT_TYPE_ERRORS:
        status = CheckStatus.FAILED
        detail = "mypy reported type errors"
    else:
        status = CheckStatus.UNAVAILABLE
        detail = f"mypy could not run (exit {result.exit_code})"
    return StepEvidence(
        step_id=step_id, kind=VerificationStepKind.TYPE_CHECK, requirement_ids=requirement_ids, status=status,
        exit_code=result.exit_code, duration_ms=result.duration_ms, stdout_excerpt=stdout_excerpt(result.stdout),
        stderr_excerpt=stderr_excerpt(result.stderr), detail=detail,
    )


__all__ = ["run_type_check"]
