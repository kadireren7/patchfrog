"""M7.3 / M7.5: deterministic patch generation and safety gates, against
real fixture repositories."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import pytest

from patchfrog.dependencies.discovery import discover_dependencies
from patchfrog.migration.domain import (
    AutoFixEligibility,
    EditOperation,
    MigrationStatus,
    StepOutcome,
)
from patchfrog.migration.edits import NotNeeded, RewriteError, apply_edits, unified_diff
from patchfrog.migration.generator import generate_patch
from patchfrog.migration.planner import plan_migration
from patchfrog.upstream.events import build_contract_change, parse_contract_document
from patchfrog.upstream.hints import parse_hints
from patchfrog.upstream.workspace import adapters_for_target, analyze_inventory, analyze_repository
from tests.support.upstream_cases import CASES_ROOT, load_case

DEMO = CASES_ROOT / "demo"


def _surface(version: str, symbols: dict[str, Any]) -> dict[str, Any]:
    return {"patchfrog_sdk_surface": 1, "package": "kit", "ecosystem": "pypi", "version": version,
            "modules": ["kit"], "symbols": symbols}


def _plan_and_patch(
    root: Path, old: dict[str, Any], new: dict[str, Any], hints: dict[str, Any] | None = None
) -> tuple[Any, Any]:
    parsed = parse_hints(hints)
    event = build_contract_change(parse_contract_document(old, ref="o"), parse_contract_document(new, ref="n"),
                                  hints=parsed)
    inventory = discover_dependencies(root, adapters=adapters_for_target(event.target))
    impact = analyze_inventory(inventory, event, hints=parsed, root=root)
    plan = plan_migration(event, impact, inventory, hints=parsed)
    return plan, generate_patch(plan, root)


# -- end-to-end on the demo fixture -----------------------------------------------


def test_demo_patch_is_a_candidate_and_idempotent(tmp_path: Path) -> None:
    case = load_case(DEMO)
    root = case.repositories["demo"]
    impact, inventory = analyze_repository(root, case.event, hints=case.hints, repository="demo")
    plan = plan_migration(case.event, impact, inventory, hints=case.hints)
    patch = generate_patch(plan, root)

    assert patch.is_candidate
    assert set(patch.modified_files) == {"app/ai/chat.py", "workers/summary.py", "requirements.txt"}
    assert all(g.passed for g in patch.safety)
    applied = {r.step_id for r in patch.step_results if r.outcome is StepOutcome.APPLIED}
    skipped = {r.step_id for r in patch.step_results if r.outcome is StepOutcome.SKIPPED}
    assert len(applied) == 7  # every AUTO_SAFE/AUTO_WITH_REVIEW step
    assert len(skipped) == 1  # the `stream` removal: HUMAN_REQUIRED

    migrated = tmp_path / "migrated"
    shutil.copytree(root, migrated)
    for rel, content in patch.new_contents.items():
        (migrated / rel).write_text(content)
    assert (migrated / "app/ai/chat.py").read_text().count("\n") == (root / "app/ai/chat.py").read_text().count("\n")

    impact2, inventory2 = analyze_repository(migrated, case.event, hints=case.hints, repository="demo")
    plan2 = plan_migration(case.event, impact2, inventory2, hints=case.hints)
    assert plan2.status is MigrationStatus.NOT_REQUIRED
    patch2 = generate_patch(plan2, migrated)
    assert patch2.unified_diff == "" and patch2.modified_files == ()


def test_unified_diff_applies_cleanly_with_git_apply(tmp_path: Path) -> None:
    pytest.importorskip("subprocess")
    import subprocess

    case = load_case(DEMO)
    root = case.repositories["demo"]
    impact, inventory = analyze_repository(root, case.event, hints=case.hints, repository="demo")
    plan = plan_migration(case.event, impact, inventory, hints=case.hints)
    patch = generate_patch(plan, root)

    work = tmp_path / "work"
    shutil.copytree(root, work)
    subprocess.run(["git", "-C", str(work), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(work), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(work), "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "x"],
                   check=True)
    patch_file = tmp_path / "patch.diff"
    patch_file.write_text(patch.unified_diff)
    result = subprocess.run(["git", "-C", str(work), "apply", "--check", str(patch_file.resolve())],
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


# -- idempotency at the edit layer -----------------------------------------------


def test_rewriters_raise_not_needed_when_already_migrated() -> None:
    from patchfrog.migration.domain import MigrationStep, MigrationStrategy, MigrationTarget
    from patchfrog.migration.python_rewrite import python_edits

    target = MigrationTarget("r", "a.py", "f", 4, "chat.create", "sdk_call", "python")
    step = MigrationStep("s1", target, MigrationStrategy.RENAME_SYMBOL, AutoFixEligibility.AUTO_SAFE, (), (), "",
                         "", "", (), "", operation=EditOperation.of("rename_call_chain", old="chat.create",
                                                                     new="responses.create"))
    already = "import kit\nc = kit.Client()\ndef f():\n    return c.responses.create()\n"
    with pytest.raises(NotNeeded):
        python_edits(step, already)


# -- safety gates --------------------------------------------------------------


def test_secret_store_files_are_never_touched_or_read(tmp_path: Path) -> None:
    old = _surface("1.0.0", {"jobs.run": {"params": {"task": {"required": True}}}})
    new = _surface("1.0.1", {"jobs.start": {"params": {"task": {"required": True}}}})
    (tmp_path / "requirements.txt").write_text("kit==1.0.0\n")
    (tmp_path / "svc.py").write_text("import kit\nc = kit.Client()\n\ndef f(x):\n    return c.jobs.run(task=x)\n")
    (tmp_path / ".env").write_text("KIT_SECRET=not-a-real-secret-abcdef123456\n")
    _plan, patch = _plan_and_patch(tmp_path, old, new, {"patchfrog_change_hints": 1,
                                                        "symbol_renames": [{"old": "jobs.run", "new": "jobs.start"}]})
    assert ".env" not in patch.modified_files
    assert "not-a-real-secret" not in patch.unified_diff


def test_conflicting_edits_at_the_same_site_fail_only_the_later_step(tmp_path: Path) -> None:
    from patchfrog.migration.domain import (
        AutoFixEligibility,
        MigrationPlan,
        MigrationStatus,
        MigrationStep,
        MigrationStrategy,
        MigrationTarget,
        ResidualRisk,
    )

    (tmp_path / "svc.py").write_text(
        "import kit\nc = kit.Client()\n\ndef f():\n    return c.jobs.run(mode='fast')\n"
    )
    target = MigrationTarget("r", "svc.py", "f", 5, "jobs.run", "sdk_call", "python")
    # Two steps that both try to replace the same literal at the same
    # site (e.g. two conflicting hinted enum mappings): the second one's
    # edit lands on the exact span the first already claimed.
    step_a = MigrationStep("a", target, MigrationStrategy.REPLACE_ENUM_VALUE, AutoFixEligibility.AUTO_SAFE, (), (),
                           "", "", "", (), "",
                           operation=EditOperation.of("replace_keyword_literal", name="mode", old="fast", new="slow"))
    step_b = MigrationStep("b", target, MigrationStrategy.REPLACE_ENUM_VALUE, AutoFixEligibility.AUTO_SAFE, (), (),
                           "", "", "", (), "",
                           operation=EditOperation.of("replace_keyword_literal", name="mode", old="fast",
                                                      new="quick"))
    plan = MigrationPlan("fp", "r", None, ("kit:pypi",), (step_a, step_b), ResidualRisk.LOW, MigrationStatus.PLANNED)
    patch = generate_patch(plan, tmp_path)
    results = {r.step_id: r for r in patch.step_results}
    assert results["a"].outcome is StepOutcome.APPLIED
    assert results["b"].outcome is StepOutcome.FAILED and "conflicts" in results["b"].detail
    assert "slow" in patch.unified_diff and "quick" not in patch.unified_diff


def test_generator_rejects_edits_outside_the_planned_file(tmp_path: Path) -> None:
    from patchfrog.migration.domain import (
        AutoFixEligibility,
        MigrationPlan,
        MigrationStatus,
        MigrationStep,
        MigrationStrategy,
        MigrationTarget,
        ResidualRisk,
    )

    (tmp_path / "svc.py").write_text("import kit\nc = kit.Client()\n\ndef f():\n    return c.jobs.run(task=1)\n")
    target = MigrationTarget("r", "svc.py", "f", 5, "jobs.run", "sdk_call", "python")
    # Renaming a chain that is not actually at line 5 -> RewriteError -> FAILED, not silently ignored.
    step = MigrationStep("s1", target, MigrationStrategy.RENAME_SYMBOL, AutoFixEligibility.AUTO_SAFE, (), (), "",
                         "", "", (), "", operation=EditOperation.of("rename_call_chain", old="jobs.other",
                                                                     new="jobs.new"))
    plan = MigrationPlan("fp", "r", None, ("kit:pypi",), (step,), ResidualRisk.LOW, MigrationStatus.PLANNED)
    patch = generate_patch(plan, tmp_path)
    assert patch.modified_files == ()
    (result,) = patch.step_results
    assert result.outcome is StepOutcome.FAILED


def test_manifest_rewrite_refuses_unparseable_range_constraints(tmp_path: Path) -> None:
    from patchfrog.migration.domain import (
        AutoFixEligibility,
        MigrationStep,
        MigrationStrategy,
        MigrationTarget,
    )
    from patchfrog.migration.manifest_rewrite import manifest_edits

    (tmp_path / "requirements.txt").write_text("kit>=1.0,<2.0\n")
    target = MigrationTarget("r", "requirements.txt", None, 1, "kit", "manifest_declaration", "manifest")
    step = MigrationStep("s1", target, MigrationStrategy.BUMP_PACKAGE_VERSION, AutoFixEligibility.AUTO_SAFE, (), (),
                         "", "", "", (), "", operation=EditOperation.of("bump_manifest_version", package="kit",
                                                                        ecosystem="pypi", version="2.0.0"))
    with pytest.raises(RewriteError, match="human decision"):
        manifest_edits(step, (tmp_path / "requirements.txt").read_text())


def test_package_json_bump_never_touches_other_fields(tmp_path: Path) -> None:
    from patchfrog.migration.domain import (
        AutoFixEligibility,
        MigrationStep,
        MigrationStrategy,
        MigrationTarget,
    )
    from patchfrog.migration.manifest_rewrite import manifest_edits

    original = '{\n  "name": "shop",\n  "dependencies": {\n    "kit": "^1.0.0",\n    "other": "^2.0.0"\n  }\n}\n'
    (tmp_path / "package.json").write_text(original)
    target = MigrationTarget("r", "package.json", None, None, "kit", "manifest_declaration", "manifest")
    step = MigrationStep("s1", target, MigrationStrategy.BUMP_PACKAGE_VERSION, AutoFixEligibility.AUTO_SAFE, (), (),
                         "", "", "", (), "", operation=EditOperation.of("bump_manifest_version", package="kit",
                                                                        ecosystem="npm", version="2.0.0"))
    edits = manifest_edits(step, original)
    result = apply_edits(original, edits)
    assert '"kit": "^2.0.0"' in result and '"other": "^2.0.0"' in result
    assert result.count("\n") == original.count("\n")


def test_unified_diff_is_empty_for_identical_text() -> None:
    assert unified_diff("a.py", "same\n", "same\n") == ""
