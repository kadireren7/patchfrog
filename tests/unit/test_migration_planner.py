"""M7 / M7.1 / M7.2: planning rules and auto-fix eligibility."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from patchfrog.dependencies.discovery import discover_dependencies
from patchfrog.migration.domain import AutoFixEligibility, MigrationStatus, MigrationStrategy
from patchfrog.migration.planner import plan_migration
from patchfrog.upstream.events import build_contract_change, parse_contract_document
from patchfrog.upstream.hints import parse_hints
from patchfrog.upstream.workspace import adapters_for_target, analyze_inventory


def _surface(version: str, symbols: dict[str, Any]) -> dict[str, Any]:
    return {"patchfrog_sdk_surface": 1, "package": "kit", "ecosystem": "pypi", "version": version,
            "modules": ["kit"], "symbols": symbols}


def _plan(tmp_path: Path, source: str, old: dict[str, Any], new: dict[str, Any],
          hints: dict[str, Any] | None = None) -> Any:
    (tmp_path / "requirements.txt").write_text(f"kit=={old['version']}\n")
    (tmp_path / "svc.py").write_text(source)
    parsed = parse_hints(hints)
    event = build_contract_change(parse_contract_document(old, ref="o"), parse_contract_document(new, ref="n"),
                                  hints=parsed)
    inventory = discover_dependencies(tmp_path, adapters=adapters_for_target(event.target))
    impact = analyze_inventory(inventory, event, hints=parsed, root=tmp_path)
    return plan_migration(event, impact, inventory, hints=parsed)


def _steps(plan: Any) -> dict[str, Any]:
    return {s.strategy.value: s for s in plan.steps}


SOURCE = "import kit\nc = kit.Client()\n\ndef send(x):\n    return c.jobs.run(task=x)\n"


def test_new_required_value_is_never_invented(tmp_path: Path) -> None:
    old = _surface("1.0.0", {"jobs.run": {"params": {"task": {"required": True}}}})
    new = _surface("1.1.0", {"jobs.run": {"params": {"task": {"required": True}, "queue": {"required": True}}}})
    step = _steps(_plan(tmp_path, SOURCE, old, new))["add_required_parameter"]
    assert step.eligibility is AutoFixEligibility.HUMAN_REQUIRED and step.operation is None
    assert "never invents" in step.residual_uncertainty

    hinted = _plan(tmp_path, SOURCE, old, new, {"patchfrog_change_hints": 1, "required_parameter_values": [
        {"symbol": "jobs.run", "parameter": "queue", "value": "default"}]})
    step = _steps(hinted)["add_required_parameter"]
    assert step.eligibility is AutoFixEligibility.AUTO_WITH_REVIEW
    assert step.operation is not None and step.operation.as_dict() == {
        "kind": "add_keyword", "params": {"name": "queue", "value": '"default"'}}


def test_one_to_one_renames_are_auto_safe_and_steps_carry_full_context(tmp_path: Path) -> None:
    old = _surface("1.0.0", {"jobs.run": {"params": {"task": {"required": True}}}})
    new = _surface("1.0.1", {"jobs.start": {"params": {"job": {"required": True}}}})
    plan = _plan(tmp_path, SOURCE, old, new, {"patchfrog_change_hints": 1,
                                               "symbol_renames": [{"old": "jobs.run", "new": "jobs.start"}],
                                               "parameter_renames": [{"symbol": "jobs.run", "old": "task",
                                                                      "new": "job"}]})
    steps = _steps(plan)
    rename = steps["rename_symbol"]
    assert rename.eligibility is AutoFixEligibility.AUTO_SAFE
    assert rename.target.location == "svc.py::send" and rename.target.line == 5
    assert rename.current_usage.startswith("sdk call `jobs.run` at svc.py:5")
    assert rename.proposed_change == "call `jobs.start` instead of `jobs.run`"
    assert steps["rename_parameter"].eligibility is AutoFixEligibility.AUTO_SAFE
    # A patch bump with every structural change automatic: safe.
    bump = steps["bump_package_version"]
    assert bump.eligibility is AutoFixEligibility.AUTO_SAFE
    assert bump.version_constraint is not None and bump.version_constraint.target == "1.0.1"
    assert plan.status is MigrationStatus.PLANNED
    assert plan.evidence["usage_sites"] and plan.evidence["diff_items"]


def test_removed_api_and_removed_argument_are_human_required(tmp_path: Path) -> None:
    old = _surface("1.0.0", {"jobs.run": {"params": {"task": {"required": True}}}})
    new = _surface("1.0.0", {})
    plan = _plan(tmp_path, SOURCE, old, new)
    assert [s.strategy for s in plan.steps] == [MigrationStrategy.REPLACE_REMOVED_API]
    assert plan.status is MigrationStatus.HUMAN_REQUIRED

    new = _surface("1.0.0", {"jobs.run": {"params": {}}})
    (step,) = _plan(tmp_path, SOURCE, old, new).steps
    assert (step.strategy, step.eligibility) == (MigrationStrategy.REMOVE_ARGUMENT,
                                                 AutoFixEligibility.HUMAN_REQUIRED)


def test_same_change_reached_twice_is_one_step(tmp_path: Path) -> None:
    old = _surface("1.0.0", {"jobs.run": {"params": {"task": {}}, "returns": {"fields": {"a": "x", "b": "y"}}}})
    new = _surface("1.0.0", {"jobs.run": {"params": {"task": {}}, "returns": {"fields": {}}}})
    (step,) = _plan(tmp_path, SOURCE, old, new).steps
    assert step.strategy is MigrationStrategy.ADAPT_RESPONSE
    assert len(step.diff_item_keys) == 2  # both removed fields, one step


def test_unaffected_repository_gets_no_plan(tmp_path: Path) -> None:
    old = _surface("1.0.0", {"jobs.run": {}, "jobs.list": {}})
    new = _surface("1.0.0", {"jobs.list": {}})
    plan = _plan(tmp_path, "import kit\nc = kit.Client()\nc.jobs.list()\n", old, new)
    assert plan.steps == () and plan.status is MigrationStatus.NOT_REQUIRED
    assert plan.notes and plan.notes[0].startswith("not affected")


def test_plan_fingerprint_changes_with_repository_state(tmp_path: Path) -> None:
    old = _surface("1.0.0", {"jobs.run": {}})
    new = _surface("1.0.0", {})
    plan = _plan(tmp_path, SOURCE, old, new)
    assert plan.fingerprint("a" * 64) == plan.fingerprint("a" * 64)
    assert plan.fingerprint("a" * 64) != plan.fingerprint("b" * 64)
