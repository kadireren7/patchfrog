"""M6.9: persisted upstream change history, DB-backed (real discovery,
real M5 registry writes, real round trips)."""

from __future__ import annotations

import shutil
import uuid
from pathlib import Path

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from patchfrog.dependencies.discovery import discover_dependencies
from patchfrog.dependencies.registry import DependencyRegistry
from patchfrog.persistence.models.upstream import (
    ExternalChangeDiffItemModel,
    ExternalChangeEventModel,
    ExternalChangeImpactModel,
)
from patchfrog.persistence.repositories import RepositoryRepository
from patchfrog.upstream.events import (
    build_contract_change,
    contract_from_registry_snapshot,
    load_contract_file,
)
from patchfrog.upstream.store import UpstreamChangeStore
from patchfrog.upstream.workspace import adapters_for_target, analyze_inventory, registry_impact
from tests.support.postgres import postgres_engine_or_skip
from tests.support.upstream_cases import CASES_ROOT, load_case


async def _repository(session_factory: async_sessionmaker[AsyncSession], name: str | None = None) -> uuid.UUID:
    name = name or f"m6-{uuid.uuid4().hex[:8]}"
    async with session_factory() as session:
        row = await RepositoryRepository().upsert(
            session, github_repository_id=uuid.uuid4().int % (2**62), owner="org", name=name,
            full_name=f"org/{name}", installation_id=0,
        )
        await session.commit()
        return row.id


async def _count(session_factory: async_sessionmaker[AsyncSession], model: type) -> int:
    async with session_factory() as session:
        return int((await session.execute(select(func.count()).select_from(model))).scalar_one())


async def test_event_ingestion_is_idempotent(session_factory: async_sessionmaker[AsyncSession]) -> None:
    case = load_case(CASES_ROOT / "demo")
    store = UpstreamChangeStore()
    async with session_factory() as session:
        first, created = await store.record_event(session, case.event)
        await session.commit()
    assert created
    items_after_first = await _count(session_factory, ExternalChangeDiffItemModel)
    assert items_after_first == len(case.event.diff)

    again = load_case(CASES_ROOT / "demo").event  # re-derived, later observation time
    async with session_factory() as session:
        second, created_again = await store.record_event(session, again)
        await session.commit()
    assert not created_again and second.id == first.id
    assert await _count(session_factory, ExternalChangeEventModel) == 1
    assert await _count(session_factory, ExternalChangeDiffItemModel) == items_after_first

    async with session_factory() as session:
        row = await store.get_event(session, case.event.fingerprint)
        assert row is not None and row.risk == "breaking" and row.last_observed_at >= row.first_observed_at
        items = await store.diff_items(session, row.id)
        assert {i.item_key for i in items} == {i.key for i in case.event.diff}
        assert await store.list_events(session, package_name="acme-ai")


async def test_impact_rows_are_per_commit_and_idempotent(session_factory: async_sessionmaker[AsyncSession]) -> None:
    case = load_case(CASES_ROOT / "demo")
    root = case.repositories["demo"]
    store = UpstreamChangeStore()
    repository_id = await _repository(session_factory)
    async with session_factory() as session:
        event_row, _ = await store.record_event(session, case.event)
        await session.commit()

    async def record(sha: str) -> list[bool]:
        inventory = discover_dependencies(root, repository="org/demo", commit_sha=sha,
                                          adapters=adapters_for_target(case.event.target))
        impact = analyze_inventory(inventory, case.event, hints=case.hints, root=root)
        async with session_factory() as session:
            await DependencyRegistry().record_inventory(session, repository_id=repository_id, inventory=inventory)
            deps = await DependencyRegistry().list_dependencies(session, repository_id=repository_id)
            written = await store.record_impact(session, event_id=event_row.id, repository_id=repository_id,
                                                impact=impact, dependency_ids={d.dependency_key: d.id for d in deps})
            await session.commit()
        return [created for _, created in written]

    assert await record("a" * 40) == [True]
    assert await record("a" * 40) == [False]  # same state: updated in place
    assert await record("b" * 40) == [True]  # new commit: history
    async with session_factory() as session:
        rows = await store.impacts_for_event(session, event_row.id)
    assert len(rows) == 2
    row = rows[0]
    assert (row.status, row.direct_count, row.transitive_count, row.related_test_count) == ("affected", 2, 3, 2)
    assert row.dependency_id is not None and row.dependency_key == "acme-ai:pypi"


async def test_cross_repository_impact_from_registry_rows(session_factory: async_sessionmaker[AsyncSession]) -> None:
    case = load_case(CASES_ROOT / "multi_repo_dependency_usage")
    store = UpstreamChangeStore()
    for name, root in case.repositories.items():
        repository_id = await _repository(session_factory, name)
        inventory = discover_dependencies(root, repository=f"org/{name}", commit_sha="c" * 40)
        async with session_factory() as session:
            await DependencyRegistry().record_inventory(session, repository_id=repository_id, inventory=inventory)
            await session.commit()
    async with session_factory() as session:
        rows, repositories, dependency_ids, repository_ids = await store.registry_dependencies(session)
    workspace = registry_impact(rows, case.event, hints=case.hints, repositories=repositories)
    status = {r.repository: r.status.value for r in workspace.repositories}
    # Registry rows carry usage sites but no source: whether a call passes
    # the renamed argument cannot be inspected, so a call of the changed
    # symbol is "uncertain" here (the local analysis, which reads the
    # call, proves checkout-service is affected). Repositories that do not
    # call the symbol at all are still provably unaffected.
    assert status == {
        "org/checkout-service": "uncertain",
        "org/crm-service": "unaffected",
        "org/docs-site": "unaffected",
        "org/billing-worker": "uncertain",
    }
    (checkout,) = [r for r in workspace.repositories if r.repository == "org/checkout-service"]
    (consumer,) = checkout.consumer_impacts[0].potential
    assert consumer.reasons == ("call arguments not inspectable (checkout.sessions.create)",)
    assert set(repository_ids) == set(repositories)
    assert ("org/checkout-service", "stripe:pypi") in dependency_ids


async def test_registry_snapshot_is_the_same_old_contract_as_the_file(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    case = load_case(CASES_ROOT / "response_schema_change")
    root = case.repositories["repo"]
    repository_id = await _repository(session_factory)
    async with session_factory() as session:
        await DependencyRegistry().record_inventory(
            session, repository_id=repository_id, inventory=discover_dependencies(root, commit_sha="d" * 40)
        )
        await session.commit()
        found = await UpstreamChangeStore().latest_contract_snapshot(
            session, repository_id=repository_id, dependency_key="openapi:contracts/orders.openapi.yaml"
        )
    assert found is not None
    dependency, snapshot = found
    assert snapshot.normalized_contract is not None
    old = contract_from_registry_snapshot(snapshot.normalized_contract, fingerprint=snapshot.fingerprint,
                                          ref="registry", version=dependency.declared_version)
    from_registry = build_contract_change(old, load_contract_file(case.directory / "new.yaml"))
    # Same change, whichever way the old contract was obtained.
    assert from_registry.fingerprint == case.event.fingerprint
    assert [i.key for i in from_registry.diff] == [i.key for i in case.event.diff]


async def test_persisted_rows_never_contain_secret_values(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    case = load_case(CASES_ROOT / "demo")
    repo = tmp_path / "repo"
    shutil.copytree(case.repositories["demo"], repo)
    fake = "PFTEST-UPSTREAM-NOT-A-SECRET-1234567890"
    (repo / ".env").write_text(f"ACME_AI_KEY={fake}\n")
    (repo / "app" / "ai" / "credentials.json").write_text(f'{{"key": "{fake}"}}\n')
    inventory = discover_dependencies(repo, adapters=adapters_for_target(case.event.target))
    impact = analyze_inventory(inventory, case.event, hints=case.hints, root=repo)
    store = UpstreamChangeStore()
    repository_id = await _repository(session_factory)
    async with session_factory() as session:
        event_row, _ = await store.record_event(session, case.event)
        await store.record_impact(session, event_id=event_row.id, repository_id=repository_id, impact=impact)
        await session.commit()
        dumped = []
        for model in (ExternalChangeEventModel, ExternalChangeDiffItemModel, ExternalChangeImpactModel):
            for row in (await session.execute(select(model))).scalars().all():
                dumped.append(repr({c.name: getattr(row, c.name) for c in model.__table__.columns}))
    assert fake not in "\n".join(dumped)


async def test_deletion_semantics_on_postgres() -> None:
    engine = await postgres_engine_or_skip("external_change_events")
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    store = UpstreamChangeStore()
    try:
        case = load_case(CASES_ROOT / "demo")
        # A per-test fingerprint: the Postgres database is shared across runs.
        event = build_contract_change(
            load_contract_file(case.directory / "old.yaml"), load_contract_file(case.directory / "new.yaml"),
            hints=case.hints,
            target=case.event.target.__class__(dependency_key=f"acme-ai:pypi:{uuid.uuid4().hex[:6]}"),
        )
        root = case.repositories["demo"]
        repository_id = await _repository(session_factory)
        inventory = discover_dependencies(root, commit_sha="e" * 40, adapters=adapters_for_target(case.event.target))
        impact = analyze_inventory(inventory, case.event, hints=case.hints, root=root)
        async with session_factory() as session:
            await DependencyRegistry().record_inventory(session, repository_id=repository_id, inventory=inventory)
            deps = await DependencyRegistry().list_dependencies(session, repository_id=repository_id)
            event_row, _ = await store.record_event(session, event)
            await store.record_impact(session, event_id=event_row.id, repository_id=repository_id, impact=impact,
                                      dependency_ids={d.dependency_key: d.id for d in deps})
            await session.commit()
            event_id = event_row.id
            dependency_id = deps[0].id

        # Deleting a dependency row keeps the historical impact (link -> NULL).
        async with session_factory() as session:
            await session.execute(text("DELETE FROM external_dependencies WHERE id = :id"), {"id": dependency_id})
            await session.commit()
        async with session_factory() as session:
            (row,) = await store.impacts_for_event(session, event_id)
            assert row.dependency_id is None and row.dependency_key == "acme-ai:pypi"

        # Deleting the repository removes its impacts; the global event stays.
        async with session_factory() as session:
            await session.execute(text("DELETE FROM repositories WHERE id = :id"), {"id": repository_id})
            await session.commit()
        async with session_factory() as session:
            assert await store.impacts_for_event(session, event_id) == []
            assert await store.get_event(session, event.fingerprint) is not None

        # Deleting the event removes its diff items.
        async with session_factory() as session:
            await session.execute(text("DELETE FROM external_change_events WHERE id = :id"), {"id": event_id})
            await session.commit()
        async with session_factory() as session:
            remaining = (await session.execute(select(func.count()).select_from(ExternalChangeDiffItemModel).where(
                ExternalChangeDiffItemModel.event_id == event_id))).scalar_one()
            assert remaining == 0
    finally:
        await engine.dispose()
