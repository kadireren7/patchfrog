"""Import-resolution verification step (M8.4/M8.2's ``IMPORTS_RESOLVE``).

Actually imports each modified module inside the sandbox, from the
patched workspace root -- a moved/renamed/removed import raises
``ImportError``/``ModuleNotFoundError`` immediately, exactly the failure
this requirement exists to catch. This executes the module's own
top-level code (same trust model as
:mod:`patchfrog.executable_verification.pytest_adapter`'s pytest
collection, which does the same thing for test modules) -- always inside
:class:`~patchfrog.executable_verification.sandbox.VerificationSandbox`,
never unsandboxed.
"""

from __future__ import annotations

import sys
from pathlib import Path, PurePosixPath

from patchfrog.executable_verification.sandbox import (
    VerificationSandbox,
    stderr_excerpt,
    stdout_excerpt,
)
from patchfrog.migration_verification.domain import CheckStatus, StepEvidence, VerificationStepKind

_PYTHON = sys.executable

_IMPORT_SCRIPT = (
    "import importlib, sys\n"
    "failures = []\n"
    "for name in sys.argv[1:]:\n"
    "    try:\n"
    "        importlib.import_module(name)\n"
    "    except Exception as exc:\n"
    "        failures.append(f'{name}: {exc.__class__.__name__}: {exc}')\n"
    "if failures:\n"
    "    print('\\n'.join(failures), file=sys.stderr)\n"
    "    sys.exit(1)\n"
)


def module_name_for_path(file_path: str) -> str | None:
    """Best-effort dotted module path for a repository-relative ``.py``
    file -- ``None`` when any path component is not a valid Python
    identifier (never guessed further; the caller must then leave that
    file's import unverified rather than build a wrong module name)."""

    path = PurePosixPath(file_path)
    if path.suffix != ".py":
        return None
    parts = list(path.parts[:-1])
    stem = path.stem
    if stem != "__init__":
        parts.append(stem)
    if not parts:
        return None
    if any(not part.isidentifier() for part in parts):
        return None
    return ".".join(parts)


async def run_import_check(
    sandbox: VerificationSandbox, *, workspace_root: Path, step_id: str, requirement_ids: tuple[str, ...],
    module_names: tuple[str, ...],
) -> StepEvidence:
    if not module_names:
        return StepEvidence(
            step_id=step_id, kind=VerificationStepKind.IMPORT_CHECK, requirement_ids=requirement_ids,
            status=CheckStatus.UNAVAILABLE, exit_code=None,
            duration_ms=0.0, stdout_excerpt="", stderr_excerpt="",
            detail="no resolvable module path for any modified file",
        )
    result = await sandbox.run([_PYTHON, "-c", _IMPORT_SCRIPT, *module_names], cwd=workspace_root)
    if result.timed_out:
        status = CheckStatus.UNAVAILABLE
        detail = "import check timed out"
    elif result.exit_code == 0:
        status = CheckStatus.PASSED
        detail = f"{len(module_names)} module(s) imported cleanly"
    else:
        status = CheckStatus.FAILED
        detail = f"import failed (exit {result.exit_code})"
    return StepEvidence(
        step_id=step_id, kind=VerificationStepKind.IMPORT_CHECK, requirement_ids=requirement_ids, status=status,
        exit_code=result.exit_code, duration_ms=result.duration_ms, stdout_excerpt=stdout_excerpt(result.stdout),
        stderr_excerpt=stderr_excerpt(result.stderr), detail=detail,
    )


__all__ = ["module_name_for_path", "run_import_check"]
