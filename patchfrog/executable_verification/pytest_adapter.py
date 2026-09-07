"""The only language/toolchain adapter implemented in v1: Python +
pytest. See ``validation/executable_verification/latest-summary.md``
section 12 for why C/C++ is deferred.

Never runs the full suite -- exactly one, already-known test file path
(from :mod:`patchfrog.executable_verification.eligibility`). Always
attempts a dependency-free ``--collect-only`` pass first (spec section
E4/latest-summary section 4): a target repository's own test
dependencies are never installed, so a missing-dependency
``ModuleNotFoundError`` at collection time is expected and must
classify as ``UNSUPPORTED``, never a crash and never a guessed
``CONFIRMED_FAILURE``. Empirically verified exit-code mapping (see
latest-summary.md section 3): collection error -> exit 2; all tests
pass -> exit 0; at least one failure -> exit 1.
"""

from __future__ import annotations

import sys
from pathlib import Path

from patchfrog.executable_verification.domain import (
    ExecutableVerificationEvidence,
    VerificationKind,
    VerificationOutcome,
)
from patchfrog.executable_verification.sandbox import (
    VerificationSandbox,
    stderr_excerpt,
    stdout_excerpt,
)

#: The exact interpreter running PatchFrog itself, resolved to an
#: absolute path -- never a bare "python3" resolved through the
#: sandbox's own PATH. This guarantees the sandboxed pytest run uses the
#: identical interpreter that has pytest installed (a base dependency of
#: this same install -- see pyproject.toml), whether that is the
#: worker image's system Python under /usr or a local/CLI dev venv
#: outside it; patchfrog.executable_verification.sandbox binds whichever
#: one is actually in use read-only for exactly this reason.
_PYTHON = sys.executable

#: pytest's own documented exit codes -- see https://docs.pytest.org/en/stable/reference/exit-codes.html
_EXIT_ALL_PASSED = 0
_EXIT_TESTS_FAILED = 1
_EXIT_INTERRUPTED = 2
_EXIT_INTERNAL_ERROR = 3
_EXIT_USAGE_ERROR = 4
_EXIT_NO_TESTS_COLLECTED = 5

_UNSUPPORTED_COLLECTION_EXIT_CODES = frozenset(
    {_EXIT_INTERRUPTED, _EXIT_INTERNAL_ERROR, _EXIT_USAGE_ERROR, _EXIT_NO_TESTS_COLLECTED}
)


async def run_pytest_verification(
    sandbox: VerificationSandbox, *, workspace_root: Path, test_target_path: str, commit_sha: str
) -> ExecutableVerificationEvidence:
    collect_result = await sandbox.run(
        [_PYTHON, "-m", "pytest", "-q", "--collect-only", test_target_path], cwd=workspace_root,
    )
    if collect_result.timed_out:
        return ExecutableVerificationEvidence(
            kind=VerificationKind.EXISTING_TARGETED_TEST, outcome=VerificationOutcome.TIMEOUT,
            test_target_path=test_target_path, commit_sha=commit_sha, exit_code=None,
            stdout_excerpt=stdout_excerpt(collect_result.stdout), stderr_excerpt=stderr_excerpt(collect_result.stderr),
            duration_ms=collect_result.duration_ms, timed_out=True,
        )
    if collect_result.exit_code != _EXIT_ALL_PASSED:
        return ExecutableVerificationEvidence(
            kind=VerificationKind.EXISTING_TARGETED_TEST, outcome=VerificationOutcome.UNSUPPORTED,
            test_target_path=test_target_path, commit_sha=commit_sha, exit_code=collect_result.exit_code,
            stdout_excerpt=stdout_excerpt(collect_result.stdout), stderr_excerpt=stderr_excerpt(collect_result.stderr),
            duration_ms=collect_result.duration_ms, timed_out=False,
        )

    run_result = await sandbox.run(
        [_PYTHON, "-m", "pytest", "-q", test_target_path], cwd=workspace_root,
    )
    if run_result.timed_out:
        outcome = VerificationOutcome.TIMEOUT
    elif run_result.exit_code == _EXIT_ALL_PASSED:
        outcome = VerificationOutcome.PASSED
    elif run_result.exit_code == _EXIT_TESTS_FAILED:
        outcome = VerificationOutcome.CONFIRMED_FAILURE
    else:
        outcome = VerificationOutcome.INCONCLUSIVE

    return ExecutableVerificationEvidence(
        kind=VerificationKind.EXISTING_TARGETED_TEST, outcome=outcome, test_target_path=test_target_path,
        commit_sha=commit_sha, exit_code=run_result.exit_code,
        stdout_excerpt=stdout_excerpt(run_result.stdout), stderr_excerpt=stderr_excerpt(run_result.stderr),
        duration_ms=collect_result.duration_ms + run_result.duration_ms, timed_out=run_result.timed_out,
    )
