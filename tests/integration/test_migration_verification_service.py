"""M8 end-to-end: real, sandboxed executable evidence against the two
bundled demo repositories.

``demo_verified`` (a single, fully-automatic consumer) demonstrates the
full VERIFIED path with real baseline-vs-patched evidence. ``demo`` (the
M7 bundled demo, which has one human-required step -- the removed
``stream`` parameter) demonstrates the HUMAN_REQUIRED path. Both reuse
:class:`~patchfrog.executable_verification.sandbox.VerificationSandbox`
directly; this test is skipped where the sandbox itself is unavailable
(see :func:`patchfrog.executable_verification.sandbox.is_sandbox_available`),
exactly like Executable Verification's own tests do.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from patchfrog.dependencies.discovery import discover_dependencies
from patchfrog.executable_verification.sandbox import is_sandbox_available
from patchfrog.migration.domain import (
    AutoFixEligibility,
    GeneratedPatch,
    MigrationPlan,
    MigrationStatus,
    MigrationStep,
    MigrationStrategy,
    MigrationTarget,
    ResidualRisk,
)
from patchfrog.migration.generator import generate_patch
from patchfrog.migration.planner import plan_migration
from patchfrog.migration_verification.domain import (
    BaselineComparisonOutcome,
    MigrationEvidenceBundle,
    VerificationOutcome,
)
from patchfrog.migration_verification.service import run_migration_verification
from patchfrog.upstream.blast_radius import BlastRadius
from patchfrog.upstream.events import build_contract_change, load_contract_file
from patchfrog.upstream.hints import load_hints
from patchfrog.upstream.workspace import adapters_for_target, analyze_inventory

DEMO_CASES_ROOT = Path(__file__).resolve().parent.parent / "fixtures" / "upstream_changes"

pytestmark = pytest.mark.skipif(not is_sandbox_available(), reason="verification sandbox unavailable on this host")


def _run(name: str) -> tuple[MigrationPlan, GeneratedPatch, MigrationEvidenceBundle]:
    root_dir = DEMO_CASES_ROOT / name
    hints = load_hints((root_dir / "hints.yaml").read_text())
    event = build_contract_change(load_contract_file(root_dir / "old.yaml"), load_contract_file(root_dir / "new.yaml"),
                                  hints=hints)
    repo_root = root_dir / "repo"
    inventory = discover_dependencies(repo_root, repository=name, adapters=adapters_for_target(event.target))
    impact = analyze_inventory(inventory, event, hints=hints, root=repo_root)
    plan = plan_migration(event, impact, inventory, hints=hints)
    patch = generate_patch(plan, repo_root)
    bundle = asyncio.run(
        run_migration_verification(
            plan=plan, patch=patch, blast_radii=impact.blast_radii, root=repo_root, commit_sha="deadbeef",
            extra_workspace_paths=(root_dir / "sdk_stub",),
        )
    )
    return plan, patch, bundle


def test_fully_automatic_migration_reaches_verified_with_strong_evidence() -> None:
    plan, _patch, bundle = _run("demo_verified")
    assert plan.status is MigrationStatus.PLANNED
    assert not plan.unresolved_steps
    assert bundle.outcome is VerificationOutcome.VERIFIED
    assert bundle.evidence_strength.value == "strong"
    assert bundle.residual_risk.value == "low"
    assert not bundle.coverage.unresolved_mandatory
    assert any(c.outcome is BaselineComparisonOutcome.BASELINE_FAIL_PATCHED_PASS for c in bundle.baseline_comparisons)


def test_migration_with_a_human_required_step_reaches_human_required() -> None:
    plan, _patch, bundle = _run("demo")
    assert plan.unresolved_steps  # the removed `stream` argument at workers/summary.py
    assert bundle.outcome is VerificationOutcome.HUMAN_REQUIRED
    # Everything automatable still checked out cleanly.
    assert not bundle.coverage.failed_mandatory


def test_evidence_bundle_is_bound_to_exact_patch_and_change_identity() -> None:
    _plan, patch, bundle = _run("demo_verified")
    assert bundle.patch_fingerprint == patch.fingerprint
    assert bundle.repository_head_sha == "deadbeef"
    other_fp = bundle.bundle_fingerprint
    assert other_fp and len(other_fp) == 64


async def _no_patch_bundle() -> MigrationEvidenceBundle:
    target = MigrationTarget("repo", "app/x.py", None, None, "x", "sdk_call", "python")
    step = MigrationStep("s1", target, MigrationStrategy.MANUAL_REVIEW, AutoFixEligibility.HUMAN_REQUIRED, (), (),
                         "", "", "", (), "")
    plan = MigrationPlan("fp", "repo", None, (), (step,), ResidualRisk.HIGH, MigrationStatus.HUMAN_REQUIRED)
    blast_radii: tuple[BlastRadius, ...] = ()
    return await run_migration_verification(
        plan=plan, patch=None, blast_radii=blast_radii, root=Path("."), commit_sha="",
    )


def test_no_patch_at_all_is_human_required_never_crashes() -> None:
    bundle = asyncio.run(_no_patch_bundle())
    assert bundle.outcome is VerificationOutcome.HUMAN_REQUIRED
