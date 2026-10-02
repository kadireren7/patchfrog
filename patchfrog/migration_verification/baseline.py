"""Baseline-vs-patched two-state verification (M8.6).

Proves the migration actually *fixed* something, rather than merely
passing a test that would have passed anyway. Both states are disposable
copies of the same original checkout -- the real working tree is never
mutated, and nothing is ever cloned or fetched over the network for this
comparison (the "baseline" is simply the pre-patch content of the exact
same repository state the plan was built from).

Bounded by policy: only ever run for a small number of the
highest-confidence (``DIRECT``) targeted tests -- see
:mod:`patchfrog.migration_verification.service` for the cap -- never for
every selected test (M8.6's own "do not require baseline execution for
every case" instruction).
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

from patchfrog.executable_verification.domain import VerificationOutcome as ExecOutcome
from patchfrog.executable_verification.pytest_adapter import run_pytest_verification
from patchfrog.executable_verification.sandbox import VerificationSandbox
from patchfrog.migration.domain import GeneratedPatch
from patchfrog.migration_verification.domain import (
    BaselineComparison,
    BaselineComparisonOutcome,
    CheckStatus,
)

_EXEC_TO_CHECK_STATUS = {
    ExecOutcome.PASSED: CheckStatus.PASSED,
    ExecOutcome.CONFIRMED_FAILURE: CheckStatus.FAILED,
    ExecOutcome.TIMEOUT: CheckStatus.UNAVAILABLE,
    ExecOutcome.UNSUPPORTED: CheckStatus.UNAVAILABLE,
    ExecOutcome.INCONCLUSIVE: CheckStatus.UNAVAILABLE,
    ExecOutcome.SANDBOX_ERROR: CheckStatus.UNAVAILABLE,
}

_OUTCOME_MATRIX = {
    (CheckStatus.FAILED, CheckStatus.PASSED): BaselineComparisonOutcome.BASELINE_FAIL_PATCHED_PASS,
    (CheckStatus.PASSED, CheckStatus.PASSED): BaselineComparisonOutcome.BASELINE_PASS_PATCHED_PASS,
    (CheckStatus.FAILED, CheckStatus.FAILED): BaselineComparisonOutcome.BASELINE_FAIL_PATCHED_FAIL_DIFFERENTLY,
    (CheckStatus.PASSED, CheckStatus.FAILED): BaselineComparisonOutcome.BASELINE_PASS_PATCHED_FAIL,
}


def _apply_patch_contents(workspace: Path, patch: GeneratedPatch) -> None:
    for relative_path, content in patch.new_contents.items():
        target = workspace / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")


async def run_baseline_comparison(
    sandbox: VerificationSandbox,
    *,
    root: Path,
    patch: GeneratedPatch,
    requirement_id: str,
    test_path: str,
    commit_sha: str,
    extra_workspace_paths: tuple[Path, ...] = (),
) -> BaselineComparison:
    if not patch.is_candidate or not patch.new_contents:
        return BaselineComparison(
            requirement_id=requirement_id, baseline_status=CheckStatus.UNAVAILABLE,
            patched_status=CheckStatus.UNAVAILABLE, outcome=BaselineComparisonOutcome.BASELINE_UNAVAILABLE,
            detail="no candidate patch content to compare against a baseline",
        )

    baseline_ws = Path(tempfile.mkdtemp(prefix="patchfrog-verify-baseline-"))
    patched_ws = Path(tempfile.mkdtemp(prefix="patchfrog-verify-patched-"))
    try:
        shutil.copytree(root, baseline_ws, dirs_exist_ok=True, symlinks=True)
        shutil.copytree(root, patched_ws, dirs_exist_ok=True, symlinks=True)
        for extra_path in extra_workspace_paths:
            shutil.copytree(extra_path, baseline_ws, dirs_exist_ok=True, symlinks=True)
            shutil.copytree(extra_path, patched_ws, dirs_exist_ok=True, symlinks=True)
        _apply_patch_contents(patched_ws, patch)

        baseline_evidence = await run_pytest_verification(
            sandbox, workspace_root=baseline_ws, test_target_path=test_path, commit_sha=commit_sha,
        )
        patched_evidence = await run_pytest_verification(
            sandbox, workspace_root=patched_ws, test_target_path=test_path, commit_sha=commit_sha,
        )
    finally:
        shutil.rmtree(baseline_ws, ignore_errors=True)
        shutil.rmtree(patched_ws, ignore_errors=True)

    baseline_status = _EXEC_TO_CHECK_STATUS[baseline_evidence.outcome]
    patched_status = _EXEC_TO_CHECK_STATUS[patched_evidence.outcome]
    outcome = _OUTCOME_MATRIX.get((baseline_status, patched_status), BaselineComparisonOutcome.BASELINE_UNAVAILABLE)
    detail = f"baseline={baseline_evidence.outcome.value}, patched={patched_evidence.outcome.value} on {test_path}"
    return BaselineComparison(
        requirement_id=requirement_id, baseline_status=baseline_status, patched_status=patched_status,
        outcome=outcome, detail=detail,
    )


__all__ = ["run_baseline_comparison"]
