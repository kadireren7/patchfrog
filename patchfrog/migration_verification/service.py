"""Top-level Migration Verification orchestrator (M8).

    migration plan + generated patch + exact repository state
    -> requirements (M8.2) -> targeted test selection (M8.3)
    -> bounded execution plan (M8.4) -> sandboxed execution (M8.5)
    -> baseline-vs-patched comparison (M8.6) -> contract verification (M8.7)
    -> regression detection (M8.8) -> evidence bundle (M8.9/M8.10)
    -> deterministic decision (M8.11)

Never calls a provider. The one entry point,
:func:`run_migration_verification`, is safe to call for any
:class:`~patchfrog.migration.domain.MigrationResult`, including one with
no patch at all (returns an evidence-free ``UNVERIFIED``/``HUMAN_REQUIRED``
bundle immediately, no sandbox touched).
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

from patchfrog.executable_verification.domain import ExecutableVerificationEvidence
from patchfrog.executable_verification.pytest_adapter import run_pytest_verification
from patchfrog.executable_verification.sandbox import VerificationSandbox, is_sandbox_available
from patchfrog.migration.domain import GeneratedPatch, MigrationPlan
from patchfrog.migration_verification import baseline as baseline_mod
from patchfrog.migration_verification import contract as contract_mod
from patchfrog.migration_verification import evidence as evidence_mod
from patchfrog.migration_verification import execution_plan as execution_plan_mod
from patchfrog.migration_verification import requirements as requirements_mod
from patchfrog.migration_verification import test_selection as test_selection_mod
from patchfrog.migration_verification.adapters.imports import module_name_for_path, run_import_check
from patchfrog.migration_verification.adapters.mypy_adapter import run_type_check
from patchfrog.migration_verification.adapters.syntax import run_syntax_check
from patchfrog.migration_verification.decision import decide_outcome
from patchfrog.migration_verification.domain import (
    MAX_STEP_STDERR_EXCERPT_BYTES,
    MAX_STEP_STDOUT_EXCERPT_BYTES,
    BaselineComparison,
    CheckStatus,
    EvidenceStrength,
    MigrationEvidenceBundle,
    MigrationVerificationPlan,
    StepEvidence,
    TestSelectionClass,
    VerificationCoverage,
    VerificationRequirementKind,
    VerificationStepKind,
)
from patchfrog.migration_verification.regression import detect_regressions
from patchfrog.upstream.blast_radius import BlastRadius

#: Never run baseline comparison for every selected test -- bounded,
#: policy-controlled (M8.6's own instruction), and only for the
#: highest-confidence (DIRECT) selections.
MAX_BASELINE_COMPARISONS = 2

_EXEC_TO_CHECK_STATUS = {
    "passed": CheckStatus.PASSED,
    "confirmed_failure": CheckStatus.FAILED,
    "timeout": CheckStatus.UNAVAILABLE,
    "unsupported": CheckStatus.UNAVAILABLE,
    "inconclusive": CheckStatus.UNAVAILABLE,
    "sandbox_error": CheckStatus.UNAVAILABLE,
}


def _bounded(text: str, limit: int) -> str:
    encoded = text.encode("utf-8")
    if len(encoded) <= limit:
        return text
    return encoded[:limit].decode("utf-8", errors="ignore") + "... (truncated)"


def _pytest_evidence_to_step(
    evidence: ExecutableVerificationEvidence, *, step_id: str, requirement_ids: tuple[str, ...]
) -> StepEvidence:
    return StepEvidence(
        step_id=step_id,
        kind=VerificationStepKind.TARGETED_UNIT_TEST,
        requirement_ids=requirement_ids,
        status=_EXEC_TO_CHECK_STATUS[evidence.outcome.value],
        exit_code=evidence.exit_code,
        duration_ms=evidence.duration_ms,
        stdout_excerpt=_bounded(evidence.stdout_excerpt, MAX_STEP_STDOUT_EXCERPT_BYTES),
        stderr_excerpt=_bounded(evidence.stderr_excerpt, MAX_STEP_STDERR_EXCERPT_BYTES),
        detail=f"pytest outcome: {evidence.outcome.value}",
    )


def _sandbox_unavailable_evidence(step_id: str, kind: VerificationStepKind, requirement_ids: tuple[str, ...]) -> StepEvidence:
    return StepEvidence(
        step_id=step_id, kind=kind, requirement_ids=requirement_ids, status=CheckStatus.UNAVAILABLE, exit_code=None,
        duration_ms=0.0, stdout_excerpt="", stderr_excerpt="",
        detail="verification sandbox unavailable on this host -- see docs/executable-verification.md",
    )


async def run_migration_verification(
    *,
    plan: MigrationPlan,
    patch: GeneratedPatch | None,
    blast_radii: tuple[BlastRadius, ...],
    root: Path,
    commit_sha: str,
    extra_workspace_paths: tuple[Path, ...] = (),
) -> MigrationEvidenceBundle:
    """``extra_workspace_paths``: directories merged into the disposable
    verification workspace *only* -- never into ``root`` itself, and
    never seen by dependency discovery/planning (M6/M7). Exists solely so
    a bundled demo/test fixture can supply a runnable stand-in for a
    third-party SDK the sandbox would otherwise lack, without that
    stand-in changing what M6/M7 detect as an external dependency usage
    site. Empty by default; real repository verification never needs it
    (the target repository's own dependencies are either present or
    genuinely unavailable, which is itself honest evidence -- see
    :data:`patchfrog.migration_verification.domain.CheckStatus.UNAVAILABLE`)."""

    if patch is None:
        empty_plan = MigrationVerificationPlan(
            change_fingerprint=plan.change_fingerprint, patch_fingerprint="", requirements=(), steps=(),
            test_selections=(),
        )
        coverage = evidence_mod.build_coverage(empty_plan, step_evidence=(), contract_checks=())
        outcome, reasons = decide_outcome(
            empty_plan, coverage=coverage, evidence_strength=_no_evidence(), regressions=(),
            has_unresolved_human_steps=bool(plan.unresolved_steps),
        )
        return evidence_mod.build_evidence_bundle(
            plan=empty_plan, repository=plan.repository, repository_head_sha=commit_sha or "",
            step_evidence=(), contract_checks=(), baseline_comparisons=(), coverage=coverage,
            evidence_strength=_no_evidence(), residual_risk=plan.residual_risk, outcome=outcome,
            outcome_reasons=reasons,
        )

    test_selections = test_selection_mod.select_targeted_tests(plan, blast_radii)
    requirements = requirements_mod.generate_requirements(plan, test_selections=test_selections)
    steps = execution_plan_mod.build_execution_plan(requirements, patch=patch, test_selections=test_selections)
    verification_plan = MigrationVerificationPlan(
        change_fingerprint=plan.change_fingerprint, patch_fingerprint=patch.fingerprint,
        requirements=requirements, steps=steps, test_selections=test_selections,
    )

    contract_checks = contract_mod.verify_contract(plan, patch, requirements)

    if not requirements:
        coverage = evidence_mod.build_coverage(verification_plan, step_evidence=(), contract_checks=contract_checks)
        outcome, reasons = decide_outcome(
            verification_plan, coverage=coverage, evidence_strength=_no_evidence(), regressions=(),
            has_unresolved_human_steps=bool(plan.unresolved_steps),
        )
        return evidence_mod.build_evidence_bundle(
            plan=verification_plan, repository=plan.repository, repository_head_sha=commit_sha or "",
            step_evidence=(), contract_checks=contract_checks, baseline_comparisons=(), coverage=coverage,
            evidence_strength=_no_evidence(), residual_risk=plan.residual_risk, outcome=outcome, outcome_reasons=reasons,
        )

    sandbox_available = is_sandbox_available()
    step_evidence: list[StepEvidence] = []
    baseline_comparisons: list[BaselineComparison] = []

    if not sandbox_available:
        for step in steps:
            step_evidence.append(_sandbox_unavailable_evidence(step.step_id, step.kind, step.requirement_ids))
    else:
        sandbox = VerificationSandbox(timeout_seconds=steps[0].timeout_seconds if steps else 30.0)
        workspace = Path(tempfile.mkdtemp(prefix="patchfrog-migration-verify-"))
        try:
            shutil.copytree(root, workspace, dirs_exist_ok=True, symlinks=True)
            for extra_path in extra_workspace_paths:
                shutil.copytree(extra_path, workspace, dirs_exist_ok=True, symlinks=True)
            for relative_path, content in patch.new_contents.items():
                target = workspace / relative_path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content, encoding="utf-8")

            python_files = tuple(sorted(f for f in patch.modified_files if f.endswith(".py")))
            for step in steps:
                if step.kind is VerificationStepKind.SYNTAX_CHECK:
                    step_evidence.append(
                        await run_syntax_check(
                            sandbox, workspace_root=workspace, step_id=step.step_id,
                            requirement_ids=step.requirement_ids, file_paths=python_files,
                        )
                    )
                elif step.kind is VerificationStepKind.IMPORT_CHECK:
                    module_names = tuple(sorted({m for f in python_files if (m := module_name_for_path(f)) is not None}))
                    step_evidence.append(
                        await run_import_check(
                            sandbox, workspace_root=workspace, step_id=step.step_id,
                            requirement_ids=step.requirement_ids, module_names=module_names,
                        )
                    )
                elif step.kind is VerificationStepKind.TYPE_CHECK:
                    step_evidence.append(
                        await run_type_check(
                            sandbox, workspace_root=workspace, step_id=step.step_id,
                            requirement_ids=step.requirement_ids, file_paths=python_files,
                        )
                    )
                elif step.kind is VerificationStepKind.TARGETED_UNIT_TEST:
                    test_path = step.command[-1]
                    pytest_evidence = await run_pytest_verification(
                        sandbox, workspace_root=workspace, test_target_path=test_path, commit_sha=commit_sha,
                    )
                    step_evidence.append(
                        _pytest_evidence_to_step(pytest_evidence, step_id=step.step_id, requirement_ids=step.requirement_ids)
                    )

            consumer_behavior_req = next(
                (r for r in requirements if r.kind is VerificationRequirementKind.CONSUMER_BEHAVIOR_PRESERVED), None
            )
            if consumer_behavior_req is not None:
                # DIRECT and TRANSITIVE only -- FALLBACK/UNKNOWN selections
                # are heuristic-strength at best (M8.3) and never trusted
                # enough to spend a baseline run on.
                strong_selections = [
                    s for s in test_selections
                    if s.classification in (TestSelectionClass.DIRECT, TestSelectionClass.TRANSITIVE)
                ]
                for selection in strong_selections[:MAX_BASELINE_COMPARISONS]:
                    baseline_comparisons.append(
                        await baseline_mod.run_baseline_comparison(
                            sandbox, root=root, patch=patch, requirement_id=consumer_behavior_req.requirement_id,
                            test_path=selection.test_path, commit_sha=commit_sha,
                            extra_workspace_paths=extra_workspace_paths,
                        )
                    )
        finally:
            shutil.rmtree(workspace, ignore_errors=True)

    step_evidence_t = tuple(step_evidence)
    baseline_comparisons_t = tuple(baseline_comparisons)
    coverage = evidence_mod.build_coverage(verification_plan, step_evidence=step_evidence_t, contract_checks=contract_checks)
    coverage = _merge_baseline_into_coverage(coverage, baseline_comparisons_t)
    evidence_strength = evidence_mod.compute_evidence_strength(
        step_evidence=step_evidence_t, contract_checks=contract_checks, baseline_comparisons=baseline_comparisons_t,
    )
    regressions = detect_regressions(
        plan, patch, step_evidence=step_evidence_t, contract_checks=contract_checks,
        baseline_comparisons=baseline_comparisons_t,
    )
    has_unresolved_human_steps = bool(plan.unresolved_steps)
    outcome, reasons = decide_outcome(
        verification_plan, coverage=coverage, evidence_strength=evidence_strength, regressions=regressions,
        has_unresolved_human_steps=has_unresolved_human_steps,
    )
    residual_risk = evidence_mod.compute_residual_risk(
        coverage, evidence_strength=evidence_strength, has_unresolved_human_steps=has_unresolved_human_steps,
    )
    return evidence_mod.build_evidence_bundle(
        plan=verification_plan, repository=plan.repository, repository_head_sha=commit_sha or "",
        step_evidence=step_evidence_t, contract_checks=contract_checks, baseline_comparisons=baseline_comparisons_t,
        coverage=coverage, evidence_strength=evidence_strength, residual_risk=residual_risk, outcome=outcome,
        outcome_reasons=reasons,
    )


def _no_evidence() -> EvidenceStrength:
    return EvidenceStrength.NONE


def _merge_baseline_into_coverage(
    coverage: VerificationCoverage, baseline_comparisons: tuple[BaselineComparison, ...],
) -> VerificationCoverage:
    if not baseline_comparisons:
        return coverage

    satisfied = set(coverage.satisfied_requirement_ids)
    failed = set(coverage.failed_requirement_ids)
    not_run = set(coverage.not_run_requirement_ids)
    unavailable = set(coverage.unavailable_requirement_ids)
    for comparison in baseline_comparisons:
        req_id = comparison.requirement_id
        not_run.discard(req_id)
        unavailable.discard(req_id)
        if comparison.patched_status is CheckStatus.PASSED:
            satisfied.add(req_id)
            failed.discard(req_id)
        elif comparison.patched_status is CheckStatus.FAILED:
            failed.add(req_id)
            satisfied.discard(req_id)
        else:
            unavailable.add(req_id)
    return VerificationCoverage(
        mandatory_requirement_ids=coverage.mandatory_requirement_ids,
        satisfied_requirement_ids=tuple(sorted(satisfied)),
        failed_requirement_ids=tuple(sorted(failed)),
        not_run_requirement_ids=tuple(sorted(not_run)),
        unavailable_requirement_ids=tuple(sorted(unavailable)),
    )


__all__ = ["MAX_BASELINE_COMPARISONS", "run_migration_verification"]
