"""Syntax-validity verification step (M8.4/M8.2's ``SYNTAX_VALID``).

Compiles every modified Python file with ``py_compile`` inside the
sandbox -- never a hand-rolled parser, and never a check against the
running PatchFrog process's own interpreter unsandboxed."""

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


async def run_syntax_check(
    sandbox: VerificationSandbox, *, workspace_root: Path, step_id: str, requirement_ids: tuple[str, ...],
    file_paths: tuple[str, ...],
) -> StepEvidence:
    if not file_paths:
        return StepEvidence(
            step_id=step_id, kind=VerificationStepKind.SYNTAX_CHECK, requirement_ids=requirement_ids,
            status=CheckStatus.NOT_RUN, exit_code=None,
            duration_ms=0.0, stdout_excerpt="", stderr_excerpt="", detail="no Python files to check",
        )
    result = await sandbox.run([_PYTHON, "-m", "py_compile", *file_paths], cwd=workspace_root)
    if result.timed_out:
        status = CheckStatus.UNAVAILABLE
        detail = "syntax check timed out"
    elif result.exit_code == 0:
        status = CheckStatus.PASSED
        detail = f"{len(file_paths)} file(s) compiled cleanly"
    else:
        status = CheckStatus.FAILED
        detail = f"py_compile failed (exit {result.exit_code})"
    return StepEvidence(
        step_id=step_id, kind=VerificationStepKind.SYNTAX_CHECK, requirement_ids=requirement_ids, status=status,
        exit_code=result.exit_code, duration_ms=result.duration_ms, stdout_excerpt=stdout_excerpt(result.stdout),
        stderr_excerpt=stderr_excerpt(result.stderr), detail=detail,
    )


__all__ = ["run_syntax_check"]
