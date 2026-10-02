"""Bounded, safe execution plan (M8.4).

Builds the fixed sequence of :class:`~patchfrog.migration_verification.domain.VerificationStep`\\ s
for a migration's requirements -- every step's ``command`` is an explicit,
pre-built argv (never a shell string, never caller-assembled at execution
time), and the whole plan is capped at
:data:`~patchfrog.migration_verification.domain.MAX_VERIFICATION_STEPS_PER_PLAN`.
Contract-specific requirements (M8.7) are deliberately **not** steps here
-- they are verified directly from the patch's own already-computed
:class:`~patchfrog.migration.domain.StepResult`\\ s in
:mod:`patchfrog.migration_verification.contract`, never a second,
string-matching re-derivation.
"""

from __future__ import annotations

import hashlib
import sys

from patchfrog.migration.domain import GeneratedPatch
from patchfrog.migration_verification.adapters.imports import module_name_for_path
from patchfrog.migration_verification.domain import (
    MAX_STEP_SECONDS,
    MAX_VERIFICATION_STEPS_PER_PLAN,
    TestSelection,
    TestSelectionClass,
    VerificationRequirement,
    VerificationRequirementKind,
    VerificationStep,
    VerificationStepKind,
)

_PYTHON = sys.executable


def _step_id(*parts: str) -> str:
    return hashlib.sha256("|".join(parts).encode()).hexdigest()[:16]


def _python_files(patch: GeneratedPatch) -> tuple[str, ...]:
    return tuple(sorted(f for f in patch.modified_files if f.endswith(".py")))


def build_execution_plan(
    requirements: tuple[VerificationRequirement, ...],
    *,
    patch: GeneratedPatch,
    test_selections: tuple[TestSelection, ...],
) -> tuple[VerificationStep, ...]:
    by_kind = {r.kind: r for r in requirements}
    steps: list[VerificationStep] = []
    python_files = _python_files(patch)

    syntax_req = by_kind.get(VerificationRequirementKind.SYNTAX_VALID)
    if syntax_req is not None and python_files:
        steps.append(
            VerificationStep(
                step_id=_step_id("syntax", syntax_req.requirement_id),
                kind=VerificationStepKind.SYNTAX_CHECK,
                requirement_ids=(syntax_req.requirement_id,),
                command=(_PYTHON, "-m", "py_compile", *python_files),
                timeout_seconds=MAX_STEP_SECONDS,
                reason="every file the patch modified must remain syntactically valid",
                blocks_verified=True,
            )
        )

    imports_req = by_kind.get(VerificationRequirementKind.IMPORTS_RESOLVE)
    if imports_req is not None and python_files:
        module_names = tuple(sorted({m for f in python_files if (m := module_name_for_path(f)) is not None}))
        steps.append(
            VerificationStep(
                step_id=_step_id("imports", imports_req.requirement_id),
                kind=VerificationStepKind.IMPORT_CHECK,
                requirement_ids=(imports_req.requirement_id,),
                command=(_PYTHON, "-c", "<import-check-script>", *module_names),
                timeout_seconds=MAX_STEP_SECONDS,
                reason="a moved/renamed/removed import must fail import, not merely fail to parse",
                blocks_verified=False,
            )
        )

    type_req = by_kind.get(VerificationRequirementKind.TYPE_CHECK_PASSES)
    if type_req is not None and python_files:
        steps.append(
            VerificationStep(
                step_id=_step_id("typecheck", type_req.requirement_id),
                kind=VerificationStepKind.TYPE_CHECK,
                requirement_ids=(type_req.requirement_id,),
                command=(_PYTHON, "-m", "mypy", "--ignore-missing-imports", *python_files),
                timeout_seconds=MAX_STEP_SECONDS,
                reason="best-effort static type check of the modified files",
                blocks_verified=False,
            )
        )

    unit_tests_req = by_kind.get(VerificationRequirementKind.UNIT_TESTS_PASS)
    if unit_tests_req is not None:
        runnable = [s for s in test_selections if s.classification is not TestSelectionClass.UNKNOWN]
        for selection in runnable:
            if len(steps) >= MAX_VERIFICATION_STEPS_PER_PLAN:
                break
            steps.append(
                VerificationStep(
                    step_id=_step_id("test", unit_tests_req.requirement_id, selection.test_path),
                    kind=VerificationStepKind.TARGETED_UNIT_TEST,
                    requirement_ids=(unit_tests_req.requirement_id,),
                    command=(_PYTHON, "-m", "pytest", "-q", selection.test_path),
                    timeout_seconds=MAX_STEP_SECONDS,
                    reason=f"{selection.classification.value}: {selection.reason}",
                    #: Only a DIRECT/TRANSITIVE selection blocks VERIFIED
                    #: on failure -- a FALLBACK selection is weak evidence
                    #: by construction (M8.3), never strong enough alone
                    #: to withhold VERIFIED on a failure that may be
                    #: unrelated to this migration.
                    blocks_verified=selection.classification in (TestSelectionClass.DIRECT, TestSelectionClass.TRANSITIVE),
                    test_classification=selection.classification,
                )
            )

    return tuple(steps[:MAX_VERIFICATION_STEPS_PER_PLAN])


__all__ = ["build_execution_plan"]
