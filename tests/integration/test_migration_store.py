"""M7.6 / M7.7: persisted migration plans and patches, DB-backed (real
discovery, real M6 event persistence, real generated patches)."""

from __future__ import annotations

import uuid

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from patchfrog.migration.domain import MigrationStatus
from patchfrog.migration.generator import generate_patch
from patchfrog.migration.planner import plan_migration
from patchfrog.migration.store import (
    MigrationStore,
    build_linkage,
    compute_base_content_fingerprint,
)
from patchfrog.persistence.models.upstream import MigrationPatchModel, MigrationPlanModel
from patchfrog.persistence.repositories import RepositoryRepository
from patchfrog.upstream.store import UpstreamChangeStore
from patchfrog.upstream.workspace import analyze_repository
from tests.support.postgres import postgres_engine_or_skip
from tests.support.upstream_cases import CASES_ROOT, load_case


async def _repository(session_factory: async_sessionmaker[AsyncSession], name: str | None = None) -> uuid.UUID:
    name = name or f"m7-{uuid.uuid4().hex[:8]}"
    async with session_factory() as session:
        row = await RepositoryRepository().upsert(
            session, github_repository_id=uuid.uuid4().int % (2**62), owner="org", name=name,
            full_name=f"org/{name}", installation_id=0,
        )
        await session.commit()
        return row.id


async def test_plan_persistence_is_idempotent_and_tracks_repository_state(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    case = load_case(CASES_ROOT / "demo")
    root = case.repositories["demo"]
    impact, inventory = analyze_repository(root, case.event, hints=case.hints, repository="demo")
    plan = plan_migration(case.event, impact, inventory, hints=case.hints)

    upstream_store = UpstreamChangeStore()
    migration_store = MigrationStore()
    repository_id = await _repository(session_factory)
    async with session_factory() as session:
        event_row, _ = await upstream_store.record_event(session, case.event)
        await session.commit()
        event_id = event_row.id

    async with session_factory() as session:
        row, base_fingerprint, created = await migration_store.record_plan(
            session, event_id=event_id, repository_id=repository_id, plan=plan, root=root
        )
        await session.commit()
    assert created and row.status == MigrationStatus.PLANNED.value
    assert row.step_count == len(plan.steps)
    assert row.auto_step_count == len(plan.automatic_steps)

    # Re-planning the exact same (unmigrated) state is a no-op write.
    async with session_factory() as session:
        row2, base_fingerprint2, created2 = await migration_store.record_plan(
            session, event_id=event_id, repository_id=repository_id, plan=plan, root=root
        )
        await session.commit()
    assert not created2 and row2.id == row.id and base_fingerprint2 == base_fingerprint
    async with session_factory() as session:
        assert await session.execute(select(func.count()).select_from(MigrationPlanModel))
        count = (await session.execute(select(func.count()).select_from(MigrationPlanModel))).scalar_one()
    assert count == 1

    # The plan fingerprint is tied to the exact repository content state:
    # a different base_content_fingerprint (as re-analysis after the
    # repository changed would compute) yields a different plan identity.
    assert plan.fingerprint(base_fingerprint) != plan.fingerprint("a" * 64)


async def test_patch_persistence_and_linkage(session_factory: async_sessionmaker[AsyncSession]) -> None:
    case = load_case(CASES_ROOT / "demo")
    root = case.repositories["demo"]
    impact, inventory = analyze_repository(root, case.event, hints=case.hints, repository="demo")
    plan = plan_migration(case.event, impact, inventory, hints=case.hints)
    patch = generate_patch(plan, root)
    assert patch.is_candidate

    upstream_store = UpstreamChangeStore()
    migration_store = MigrationStore()
    repository_id = await _repository(session_factory)
    async with session_factory() as session:
        event_row, _ = await upstream_store.record_event(session, case.event)
        plan_row, base_fingerprint, _ = await migration_store.record_plan(
            session, event_id=event_row.id, repository_id=repository_id, plan=plan, root=root
        )
        linkage = build_linkage(plan, patch, base_content_fingerprint=base_fingerprint,
                                plan_fingerprint=plan_row.plan_fingerprint)
        patch_row, created = await migration_store.record_patch(
            session, plan_id=plan_row.id, patch=patch, linkage=linkage
        )
        await session.commit()

    assert created and patch_row.status == "candidate" and patch_row.origin == "deterministic"
    assert patch_row.unified_diff == patch.unified_diff
    assert linkage.change_fingerprint == case.event.fingerprint
    assert linkage.repository == "demo"
    assert set(linkage.dependency_keys) == {"acme-ai:pypi"}
    assert linkage.diff_item_keys  # traces back to the originating diff

    # Re-recording the identical patch for the same plan is a no-op write.
    async with session_factory() as session:
        patch_row2, created2 = await migration_store.record_patch(
            session, plan_id=plan_row.id, patch=patch, linkage=linkage
        )
    assert not created2 and patch_row2.id == patch_row.id
    async with session_factory() as session:
        count = (await session.execute(select(func.count()).select_from(MigrationPatchModel))).scalar_one()
    assert count == 1

    async with session_factory() as session:
        plans = await migration_store.plans_for_event(session, event_row.id)
        patches = await migration_store.patches_for_plan(session, plan_row.id)
    assert [p.id for p in plans] == [plan_row.id]
    assert [p.id for p in patches] == [patch_row.id]


async def test_base_content_fingerprint_changes_with_repository_content(tmp_path: object) -> None:
    from pathlib import Path

    from patchfrog.migration.domain import (
        AutoFixEligibility,
        MigrationPlan,
        MigrationStep,
        MigrationStrategy,
        MigrationTarget,
        ResidualRisk,
    )

    root = Path(str(tmp_path))
    (root / "svc.py").write_text("x = 1\n")
    target = MigrationTarget("r", "svc.py", None, 1, "x", "sdk_call", "python")
    step = MigrationStep("s1", target, MigrationStrategy.MANUAL_REVIEW, AutoFixEligibility.HUMAN_REQUIRED, (), (),
                         "", "", "", (), "")
    plan = MigrationPlan("fp", "r", None, (), (step,), ResidualRisk.LOW, MigrationStatus.HUMAN_REQUIRED)
    fp1 = compute_base_content_fingerprint(root, plan)
    (root / "svc.py").write_text("x = 2\n")
    fp2 = compute_base_content_fingerprint(root, plan)
    assert fp1 != fp2
    (root / "svc.py").write_text("x = 1\n")
    fp3 = compute_base_content_fingerprint(root, plan)
    assert fp1 == fp3

    # A missing file contributes a stable sentinel, never a crash.
    (root / "svc.py").unlink()
    fp_missing = compute_base_content_fingerprint(root, plan)
    assert fp_missing != fp1


async def test_deletion_semantics_on_postgres() -> None:
    engine = await postgres_engine_or_skip("migration_plans")
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    upstream_store = UpstreamChangeStore()
    migration_store = MigrationStore()
    try:
        case = load_case(CASES_ROOT / "demo")
        root = case.repositories["demo"]
        # A per-test target keeps this run's fingerprints private to this
        # test in the shared database.
        from dataclasses import replace as dc_replace

        event = dc_replace(
            case.event, target=dc_replace(case.event.target, dependency_key=f"acme-ai:pypi:{uuid.uuid4().hex[:6]}")
        )
        impact, inventory = analyze_repository(root, event, hints=case.hints, repository="demo")
        plan = plan_migration(event, impact, inventory, hints=case.hints)
        patch = generate_patch(plan, root)
        repository_id = await _repository(session_factory)

        async with session_factory() as session:
            event_row, _ = await upstream_store.record_event(session, event)
            plan_row, base_fingerprint, _ = await migration_store.record_plan(
                session, event_id=event_row.id, repository_id=repository_id, plan=plan, root=root
            )
            linkage = build_linkage(plan, patch, base_content_fingerprint=base_fingerprint,
                                    plan_fingerprint=plan_row.plan_fingerprint)
            patch_row, _ = await migration_store.record_patch(session, plan_id=plan_row.id, patch=patch,
                                                              linkage=linkage)
            await session.commit()
            plan_id, patch_id = plan_row.id, patch_row.id

        # Deleting the plan cascades to its patches.
        async with session_factory() as session:
            await session.execute(text("DELETE FROM migration_plans WHERE id = :id"), {"id": plan_id})
            await session.commit()
        async with session_factory() as session:
            remaining = (await session.execute(
                select(func.count()).select_from(MigrationPatchModel).where(MigrationPatchModel.id == patch_id)
            )).scalar_one()
            assert remaining == 0

        # A repository/event deletion cascades to plans (re-create to verify).
        async with session_factory() as session:
            plan_row2, _, _ = await migration_store.record_plan(
                session, event_id=event_row.id, repository_id=repository_id, plan=plan, root=root
            )
            await session.commit()
            plan_id2 = plan_row2.id
        async with session_factory() as session:
            await session.execute(text("DELETE FROM repositories WHERE id = :id"), {"id": repository_id})
            await session.commit()
        async with session_factory() as session:
            remaining = (await session.execute(
                select(func.count()).select_from(MigrationPlanModel).where(MigrationPlanModel.id == plan_id2)
            )).scalar_one()
            assert remaining == 0

        # The event itself is global (not tied to any one repository) and
        # outlives a repository delete -- clean it up explicitly, exactly
        # as tests/integration/test_upstream_store.py's own equivalent
        # Postgres test does, so this shared database does not accumulate
        # rows across runs.
        async with session_factory() as session:
            await session.execute(text("DELETE FROM external_change_events WHERE id = :id"), {"id": event_row.id})
            await session.commit()
    finally:
        await engine.dispose()
