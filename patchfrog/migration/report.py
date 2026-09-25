"""Machine-readable (JSON) and human-readable views of M7 results.

The same dictionaries back ``--json`` CLI output and the bounded JSON
columns in :mod:`patchfrog.migration.store`, exactly mirroring
:mod:`patchfrog.upstream.report`'s role for M6.
"""

from __future__ import annotations

from typing import Any

from patchfrog.migration.domain import (
    GeneratedPatch,
    MigrationPlan,
    MigrationStep,
    MigrationTarget,
    PatchLinkage,
    SafetyGateResult,
    StepResult,
    VersionConstraint,
)


def target_to_dict(target: MigrationTarget) -> dict[str, Any]:
    return {
        "file": target.file_path, "symbol": target.symbol, "line": target.line, "usage_token": target.usage_token,
        "evidence_type": target.evidence_type, "language": target.language, "location": target.location,
    }


def version_constraint_to_dict(constraint: VersionConstraint) -> dict[str, Any]:
    return {"manifest": constraint.manifest, "package": constraint.package, "ecosystem": constraint.ecosystem,
            "current": constraint.current, "target": constraint.target}


def step_to_dict(step: MigrationStep) -> dict[str, Any]:
    return {
        "step_id": step.step_id,
        "target": target_to_dict(step.target),
        "strategy": step.strategy.value,
        "eligibility": step.eligibility.value,
        "auto_fix": step.auto_fix,
        "diff_items": list(step.diff_item_keys),
        "diff_item_kinds": list(step.diff_item_kinds),
        "current_usage": step.current_usage,
        "required_change": step.required_change,
        "proposed_change": step.proposed_change,
        "tests_to_update": list(step.tests_to_update),
        "residual_uncertainty": step.residual_uncertainty,
        "operation": step.operation.as_dict() if step.operation else None,
        "version_constraint": version_constraint_to_dict(step.version_constraint) if step.version_constraint
        else None,
    }


def plan_to_dict(plan: MigrationPlan) -> dict[str, Any]:
    return {
        "change_fingerprint": plan.change_fingerprint,
        "repository": plan.repository,
        "commit_sha": plan.commit_sha,
        "dependency_keys": list(plan.dependency_keys),
        "status": plan.status.value,
        "residual_risk": plan.residual_risk.value,
        "steps": [step_to_dict(s) for s in plan.steps],
        "automatic_step_count": len(plan.automatic_steps),
        "unresolved_step_count": len(plan.unresolved_steps),
        "evidence": {k: list(v) for k, v in plan.evidence.items()},
        "notes": list(plan.notes),
        "engine_version": plan.engine_version,
    }


def step_result_to_dict(result: StepResult) -> dict[str, Any]:
    return {"step_id": result.step_id, "outcome": result.outcome.value, "detail": result.detail,
            "edits": result.edits}


def safety_gate_to_dict(gate: SafetyGateResult) -> dict[str, Any]:
    return {"gate": gate.gate, "passed": gate.passed, "detail": gate.detail}


def linkage_to_dict(linkage: PatchLinkage) -> dict[str, Any]:
    return {
        "change_fingerprint": linkage.change_fingerprint,
        "dependency_keys": list(linkage.dependency_keys),
        "diff_items": list(linkage.diff_item_keys),
        "usage_sites": list(linkage.usage_site_keys),
        "repository": linkage.repository,
        "base_commit_sha": linkage.base_commit_sha,
        "base_content_fingerprint": linkage.base_content_fingerprint,
        "plan_fingerprint": linkage.plan_fingerprint,
        "patch_fingerprint": linkage.patch_fingerprint,
        "engine_version": linkage.engine_version,
    }


def patch_to_dict(patch: GeneratedPatch, *, include_diff: bool = True) -> dict[str, Any]:
    return {
        "origin": patch.origin.value,
        "fingerprint": patch.fingerprint,
        "is_candidate": patch.is_candidate,
        "modified_files": list(patch.modified_files),
        "step_results": [step_result_to_dict(r) for r in patch.step_results],
        "safety": [safety_gate_to_dict(g) for g in patch.safety],
        "unified_diff": patch.unified_diff if include_diff else None,
    }


def render_plan_text(plan: MigrationPlan) -> str:
    if not plan.steps:
        lines = [f"Migration for {plan.repository}: {plan.status.value.upper()}"]
        lines.extend(f"  {note}" for note in plan.notes)
        return "\n".join(lines) + "\n"
    lines = [
        f"Migration plan for {plan.repository}: {plan.status.value.upper()} "
        f"(residual risk: {plan.residual_risk.value})",
        f"  {len(plan.automatic_steps)} automatic step(s), {len(plan.unresolved_steps)} needing a human",
    ]
    for step in plan.steps:
        marker = "auto" if step.auto_fix else "human"
        lines.append(f"  [{marker}:{step.eligibility.value}] {step.strategy.value} @ {step.target.location}")
        lines.append(f"      current:  {step.current_usage}")
        lines.append(f"      required: {step.required_change}")
        lines.append(f"      proposed: {step.proposed_change}")
        if step.residual_uncertainty and step.residual_uncertainty != "none":
            lines.append(f"      residual: {step.residual_uncertainty}")
        if step.tests_to_update:
            lines.append(f"      tests:    {', '.join(step.tests_to_update)}")
    for note in plan.notes:
        lines.append(f"  note: {note}")
    return "\n".join(lines) + "\n"


def render_patch_text(patch: GeneratedPatch) -> str:
    lines = [f"Patch ({patch.origin.value}): {'candidate' if patch.is_candidate else 'not a candidate'}"]
    for result in patch.step_results:
        lines.append(f"  {result.outcome.value:11} {result.step_id}: {result.detail}")
    for gate in patch.safety:
        lines.append(f"  gate {'OK ' if gate.passed else 'FAIL'} {gate.gate}: {gate.detail}")
    return "\n".join(lines) + "\n" + patch.unified_diff


__all__ = [
    "linkage_to_dict",
    "patch_to_dict",
    "plan_to_dict",
    "render_patch_text",
    "render_plan_text",
    "safety_gate_to_dict",
    "step_result_to_dict",
    "step_to_dict",
    "target_to_dict",
    "version_constraint_to_dict",
]
