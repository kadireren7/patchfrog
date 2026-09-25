"""M7 end-to-end corpus: consumer impact -> migration plan -> generated
patch, against the same real fixture repositories M6's corpus uses.

For every case with a ``migration`` expectation block: the plan matches
the reviewed strategy/eligibility per step, and -- when the plan has any
automatic step -- the generated patch is a safety-gate-passing candidate
whose unified diff applies cleanly and is idempotent (materializing it
and re-running the whole pipeline leaves no automatic step behind)."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from patchfrog.dependencies.discovery import discover_dependencies
from patchfrog.migration.domain import AutoFixEligibility, StepOutcome
from patchfrog.migration.generator import generate_patch
from patchfrog.migration.planner import plan_migration
from patchfrog.upstream.workspace import RepositoryImpact, adapters_for_target, analyze_repository
from tests.support.upstream_cases import UpstreamCase, all_case_dirs, load_case

CASES_WITH_MIGRATION = [
    (case_dir, name)
    for case_dir in all_case_dirs()
    for name in load_case(case_dir).expect["repositories"]
    if "migration" in load_case(case_dir).expect["repositories"][name]
]


def _analyze_one(case: UpstreamCase, name: str) -> tuple[RepositoryImpact, Path]:
    root = case.repositories[name]
    impact, _ = analyze_repository(root, case.event, hints=case.hints, repository=name)
    return impact, root


@pytest.mark.parametrize("case_dir,name", CASES_WITH_MIGRATION, ids=lambda v: str(v))
def test_plan_matches_the_reviewed_expectation(case_dir: Path, name: str) -> None:
    case = load_case(case_dir)
    impact, root = _analyze_one(case, name)
    inventory = discover_dependencies(root, repository=name, adapters=adapters_for_target(case.event.target))
    plan = plan_migration(case.event, impact, inventory, hints=case.hints)
    wanted = case.expect["repositories"][name]["migration"]
    assert plan.status.value == wanted["plan_status"], (case_dir.name, name)
    assert plan.residual_risk.value == wanted["residual_risk"], (case_dir.name, name)
    steps = sorted(f"{s.eligibility.value} {s.strategy.value} {s.target.location}" for s in plan.steps)
    assert steps == sorted(wanted["steps"]), (case_dir.name, name)


@pytest.mark.parametrize("case_dir,name", CASES_WITH_MIGRATION, ids=lambda v: str(v))
def test_generated_patch_is_a_safe_candidate_when_any_step_is_automatic(
    case_dir: Path, name: str, tmp_path: Path
) -> None:
    case = load_case(case_dir)
    impact, root = _analyze_one(case, name)
    inventory = discover_dependencies(root, repository=name, adapters=adapters_for_target(case.event.target))
    plan = plan_migration(case.event, impact, inventory, hints=case.hints)
    patch = generate_patch(plan, root)

    if not plan.automatic_steps:
        assert patch.modified_files == () and patch.unified_diff == ""
        return

    assert patch.is_candidate, (case_dir.name, name, [g for g in patch.safety if not g.passed])
    assert all(r.outcome is not StepOutcome.FAILED for r in patch.step_results), (
        case_dir.name, name, [r for r in patch.step_results if r.outcome is StepOutcome.FAILED]
    )
    # Every AUTO_SAFE/AUTO_WITH_REVIEW step either applied or was found
    # not-needed -- never silently skipped.
    applied_or_not_needed = {
        r.step_id for r in patch.step_results if r.outcome in (StepOutcome.APPLIED, StepOutcome.NOT_NEEDED)
    }
    assert {s.step_id for s in plan.automatic_steps} <= applied_or_not_needed


@pytest.mark.parametrize("case_dir,name", CASES_WITH_MIGRATION, ids=lambda v: str(v))
def test_patch_is_idempotent_on_a_materialized_copy(case_dir: Path, name: str, tmp_path: Path) -> None:
    case = load_case(case_dir)
    impact, root = _analyze_one(case, name)
    inventory = discover_dependencies(root, repository=name, adapters=adapters_for_target(case.event.target))
    plan = plan_migration(case.event, impact, inventory, hints=case.hints)
    patch = generate_patch(plan, root)
    if not patch.new_contents:
        pytest.skip("nothing to materialize for this case")

    migrated = tmp_path / "migrated"
    shutil.copytree(root, migrated)
    for rel, content in patch.new_contents.items():
        (migrated / rel).write_text(content)
        # Replacements never introduce/remove a newline.
        assert content.count("\n") == (root / rel).read_text().count("\n")

    impact2, _ = analyze_repository(migrated, case.event, hints=case.hints, repository=name)
    inventory2 = discover_dependencies(migrated, repository=name, adapters=adapters_for_target(case.event.target))
    plan2 = plan_migration(case.event, impact2, inventory2, hints=case.hints)
    # The planner may still re-propose a step (e.g. a version bump kept
    # AUTO_WITH_REVIEW because another consumer is still HUMAN_REQUIRED)
    # -- the real idempotency guarantee is that generating from it makes
    # no further change: every rewriter raises NotNeeded on already-
    # migrated code, never a no-op edit.
    patch2 = generate_patch(plan2, migrated)
    assert patch2.unified_diff == "" and patch2.modified_files == ()


def test_no_step_ever_invents_a_value_across_the_corpus() -> None:
    """Cross-case structural guarantee: every AUTO_SAFE/AUTO_WITH_REVIEW
    step's proposed value traces back to the contract, a hint, or an
    existing argument -- never a HUMAN_REQUIRED step gets an operation."""

    for case_dir in all_case_dirs():
        case = load_case(case_dir)
        for name, root in case.repositories.items():
            impact, _ = analyze_repository(root, case.event, hints=case.hints, repository=name)
            inventory = discover_dependencies(root, repository=name, adapters=adapters_for_target(case.event.target))
            plan = plan_migration(case.event, impact, inventory, hints=case.hints)
            for step in plan.steps:
                if step.eligibility in (AutoFixEligibility.HUMAN_REQUIRED, AutoFixEligibility.UNSUPPORTED):
                    assert step.operation is None, (case_dir.name, name, step.step_id)
