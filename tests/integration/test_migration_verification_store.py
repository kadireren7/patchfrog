"""M8.9 persistence: idempotent evidence-bundle storage, DB-backed."""

from __future__ import annotations

import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from patchfrog.dependencies.discovery import discover_dependencies
from patchfrog.migration.domain import (
    AutoFixEligibility,
    EditOperation,
    GeneratedPatch,
    MigrationPlan,
    MigrationStatus,
    MigrationStep,
    MigrationStrategy,
    MigrationTarget,
    PatchOrigin,
    ResidualRisk,
    SafetyGateResult,
    StepOutcome,
    StepResult,
)
from patchfrog.migration.generator import generate_patch
from patchfrog.migration.planner import plan_migration
from patchfrog.migration.store import MigrationStore, build_linkage
from patchfrog.migration_verification.decision import decide_outcome
from patchfrog.migration_verification.domain import (
    EvidenceStrength,
    MigrationEvidenceBundle,
    MigrationVerificationPlan,
)
from patchfrog.migration_verification.evidence import (
    build_coverage,
    build_evidence_bundle,
    compute_residual_risk,
)
from patchfrog.migration_verification.store import MigrationVerificationStore
from patchfrog.persistence.models.migration_verification import MigrationVerificationRunModel
from patchfrog.persistence.repositories import RepositoryRepository
from patchfrog.upstream.store import UpstreamChangeStore
from patchfrog.upstream.workspace import adapters_for_target, analyze_inventory
from tests.support.upstream_cases import CASES_ROOT, load_case


async def _repository(session_factory: async_sessionmaker[AsyncSession]) -> uuid.UUID:
    name = f"m8-{uuid.uuid4().hex[:8]}"
    async with session_factory() as session:
        row = await RepositoryRepository().upsert(
            session, github_repository_id=uuid.uuid4().int % (2**62), owner="org", name=name,
            full_name=f"org/{name}", installation_id=0,
        )
        await session.commit()
        return row.id


def _target() -> MigrationTarget:
    return MigrationTarget("repo", "app/chat.py", "generate_reply", 3, "chat.create", "sdk_call", "python")


def _bundle(
    *, change_fingerprint: str, patch_fingerprint: str, outcome_strength: EvidenceStrength
) -> MigrationEvidenceBundle:
    plan = MigrationVerificationPlan(
        change_fingerprint=change_fingerprint, patch_fingerprint=patch_fingerprint, requirements=(), steps=(),
        test_selections=(),
    )
    coverage = build_coverage(plan, step_evidence=(), contract_checks=())
    outcome, reasons = decide_outcome(
        plan, coverage=coverage, evidence_strength=outcome_strength, regressions=(), has_unresolved_human_steps=False,
    )
    residual_risk = compute_residual_risk(coverage, evidence_strength=outcome_strength, has_unresolved_human_steps=False)
    return build_evidence_bundle(
        plan=plan, repository="org/repo", repository_head_sha="deadbeef", step_evidence=(), contract_checks=(),
        baseline_comparisons=(), coverage=coverage, evidence_strength=outcome_strength, residual_risk=residual_risk,
        outcome=outcome, outcome_reasons=reasons,
    )


async def test_verification_run_persistence_is_idempotent(session_factory: async_sessionmaker[AsyncSession]) -> None:
    case = load_case(CASES_ROOT / "demo")
    root = case.repositories["demo"]
    inventory = discover_dependencies(root, repository="demo", adapters=adapters_for_target(case.event.target))
    impact = analyze_inventory(inventory, case.event, hints=case.hints, root=root)
    plan = plan_migration(case.event, impact, inventory, hints=case.hints)
    patch = generate_patch(plan, root)

    upstream_store = UpstreamChangeStore()
    migration_store = MigrationStore()
    verification_store = MigrationVerificationStore()
    repository_id = await _repository(session_factory)

    async with session_factory() as session:
        event_row, _ = await upstream_store.record_event(session, case.event)
        plan_row, base_fingerprint, _ = await migration_store.record_plan(
            session, event_id=event_row.id, repository_id=repository_id, plan=plan, root=root
        )
        linkage = build_linkage(plan, patch, base_content_fingerprint=base_fingerprint,
                                plan_fingerprint=plan_row.plan_fingerprint)
        patch_row, _ = await migration_store.record_patch(session, plan_id=plan_row.id, patch=patch, linkage=linkage)
        await session.commit()
        patch_id = patch_row.id

    bundle = _bundle(
        change_fingerprint=plan.change_fingerprint, patch_fingerprint=patch.fingerprint,
        outcome_strength=EvidenceStrength.NONE,
    )

    async with session_factory() as session:
        row, created = await verification_store.record_run(session, patch_id=patch_id, bundle=bundle)
        await session.commit()
    assert created
    assert row.outcome == bundle.outcome.value

    # Re-recording the identical (patch, verification-plan-fingerprint)
    # pair is a no-op write -- idempotent, never a duplicate row.
    async with session_factory() as session:
        row2, created2 = await verification_store.record_run(session, patch_id=patch_id, bundle=bundle)
        await session.commit()
    assert not created2 and row2.id == row.id
    async with session_factory() as session:
        count = (
            await session.execute(
                select(func.count()).select_from(MigrationVerificationRunModel).where(
                    MigrationVerificationRunModel.patch_id == patch_id
                )
            )
        ).scalar_one()
    assert count == 1

    async with session_factory() as session:
        latest = await verification_store.latest_for_patch(session, patch_id=patch_id)
    assert latest is not None and latest.id == row.id


async def test_a_different_patch_fingerprint_never_reuses_old_evidence(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """M9.8's integrity requirement starts at the storage layer: a
    verification run is keyed by (patch_id, verification-plan
    fingerprint) -- a different generated patch is a different
    ``patch_id`` by construction (migration_patches' own unique
    constraint), so its evidence can never collide with another patch's."""

    target = _target()
    step = MigrationStep("s1", target, MigrationStrategy.RENAME_SYMBOL, AutoFixEligibility.AUTO_SAFE, (), (), "o",
                         "n", "n", (), "", operation=EditOperation.of("op"))
    plan = MigrationPlan("fp", "org/repo", "sha", (), (step,), ResidualRisk.LOW, MigrationStatus.PLANNED)
    patch_a = GeneratedPatch(PatchOrigin.DETERMINISTIC, "diff-a", ("app/chat.py",), {"app/chat.py": "a"},
                             (StepResult("s1", StepOutcome.APPLIED, "d"),), (SafetyGateResult("g", True, "ok"),))
    patch_b = GeneratedPatch(PatchOrigin.DETERMINISTIC, "diff-b", ("app/chat.py",), {"app/chat.py": "b"},
                             (StepResult("s1", StepOutcome.APPLIED, "d"),), (SafetyGateResult("g", True, "ok"),))
    assert patch_a.fingerprint != patch_b.fingerprint

    upstream_store = UpstreamChangeStore()
    migration_store = MigrationStore()
    verification_store = MigrationVerificationStore()
    repository_id = await _repository(session_factory)
    case = load_case(CASES_ROOT / "demo")

    async with session_factory() as session:
        event_row, _ = await upstream_store.record_event(session, case.event)
        plan_row, base_fingerprint, _ = await migration_store.record_plan(
            session, event_id=event_row.id, repository_id=repository_id, plan=plan, root=case.repositories["demo"],
        )
        linkage_a = build_linkage(plan, patch_a, base_content_fingerprint=base_fingerprint,
                                  plan_fingerprint=plan_row.plan_fingerprint)
        linkage_b = build_linkage(plan, patch_b, base_content_fingerprint=base_fingerprint,
                                  plan_fingerprint=plan_row.plan_fingerprint)
        patch_row_a, _ = await migration_store.record_patch(session, plan_id=plan_row.id, patch=patch_a, linkage=linkage_a)
        patch_row_b, _ = await migration_store.record_patch(session, plan_id=plan_row.id, patch=patch_b, linkage=linkage_b)
        await session.commit()

    bundle_a = _bundle(change_fingerprint=plan.change_fingerprint, patch_fingerprint=patch_a.fingerprint,
                       outcome_strength=EvidenceStrength.STRONG)
    bundle_b = _bundle(change_fingerprint=plan.change_fingerprint, patch_fingerprint=patch_b.fingerprint,
                       outcome_strength=EvidenceStrength.NONE)

    async with session_factory() as session:
        row_a, _ = await verification_store.record_run(session, patch_id=patch_row_a.id, bundle=bundle_a)
        row_b, _ = await verification_store.record_run(session, patch_id=patch_row_b.id, bundle=bundle_b)
        await session.commit()

    assert row_a.id != row_b.id
    assert row_a.patch_fingerprint != row_b.patch_fingerprint
