"""M5.5: the persistent dependency/contract registry, DB-backed (real
discovery over a real fixture checkout, real persistence -- never
hand-built domain objects standing in for a round trip)."""

from __future__ import annotations

import json
import shutil
import uuid
from pathlib import Path

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from patchfrog.dependencies import discover_dependencies
from patchfrog.dependencies.registry import (
    STATUS_ACTIVE,
    STATUS_REMOVED,
    DependencyRegistry,
    RegistryWriteResult,
)
from patchfrog.persistence.models.dependency import (
    ExternalContractSnapshotModel,
    ExternalDependencyModel,
    ExternalDependencyUsageSiteModel,
)
from patchfrog.persistence.repositories import RepositoryRepository
from tests.support.postgres import postgres_engine_or_skip

MIXED = Path(__file__).resolve().parents[1] / "fixtures" / "dependencies" / "mixed_repo"


async def _repository(session_factory: async_sessionmaker[AsyncSession]) -> uuid.UUID:
    name = f"m5-{uuid.uuid4().hex[:8]}"
    async with session_factory() as session:
        row = await RepositoryRepository().upsert(
            session, github_repository_id=uuid.uuid4().int % (2**62), owner="test", name=name,
            full_name=f"test/{name}", installation_id=0,
        )
        await session.commit()
        return row.id


async def _record(
    session_factory: async_sessionmaker[AsyncSession], repository_id: uuid.UUID, root: Path, sha: str
) -> RegistryWriteResult:
    inventory = discover_dependencies(root, repository="test/m5", commit_sha=sha)
    async with session_factory() as session:
        result = await DependencyRegistry().record_inventory(session, repository_id=repository_id, inventory=inventory)
        await session.commit()
    return result


async def _count(session_factory: async_sessionmaker[AsyncSession], model: type) -> int:
    async with session_factory() as session:
        return int((await session.execute(select(func.count()).select_from(model))).scalar_one())


async def _dependency(
    session_factory: async_sessionmaker[AsyncSession], repository_id: uuid.UUID, key: str
) -> ExternalDependencyModel:
    async with session_factory() as session:
        row = (await session.execute(select(ExternalDependencyModel).where(
            ExternalDependencyModel.repository_id == repository_id, ExternalDependencyModel.dependency_key == key
        ))).scalar_one()
        return row


async def test_registry_lifecycle(session_factory: async_sessionmaker[AsyncSession], tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    shutil.copytree(MIXED, repo)
    repository_id = await _repository(session_factory)

    # 1. First discovery.
    first = await _record(session_factory, repository_id, repo, "a" * 40)
    assert first.created == 5 and first.contract_snapshots_created == 5
    dependencies = await _count(session_factory, ExternalDependencyModel)
    sites = await _count(session_factory, ExternalDependencyUsageSiteModel)
    snapshots = await _count(session_factory, ExternalContractSnapshotModel)
    openai = await _dependency(session_factory, repository_id, "openai:pypi")
    assert (openai.declared_version, openai.status, openai.usage_site_count) == ("==1.40.0", STATUS_ACTIVE, 8)

    # 2. Repeated identical discovery: idempotent -- no new rows at all.
    again = await _record(session_factory, repository_id, repo, "a" * 40)
    assert (again.created, again.updated, again.unchanged, again.contract_snapshots_created) == (0, 0, 5, 0)
    assert again.usage_site_sets_rewritten == 0
    assert (await _count(session_factory, ExternalDependencyModel)) == dependencies
    assert (await _count(session_factory, ExternalDependencyUsageSiteModel)) == sites
    assert (await _count(session_factory, ExternalContractSnapshotModel)) == snapshots

    # 3. Dependency version change: row updated in place, new contract snapshot, history kept.
    requirements = repo / "requirements.txt"
    requirements.write_text(requirements.read_text().replace("openai==1.40.0", "openai==1.52.0"))
    bumped = await _record(session_factory, repository_id, repo, "b" * 40)
    assert bumped.updated == 1 and bumped.contract_snapshots_created == 1
    openai = await _dependency(session_factory, repository_id, "openai:pypi")
    assert openai.declared_version == "==1.52.0" and openai.last_observed_commit_sha == "b" * 40
    async with session_factory() as session:
        history = await DependencyRegistry().contract_history(session, dependency_id=openai.id)
    assert [h.declared_version for h in history] == ["==1.40.0", "==1.52.0"]

    # 4. OpenAPI contract fingerprint change (auth scheme), prose-only edit does not count.
    spec = repo / "openapi.yaml"
    spec.write_text(spec.read_text().replace("Internal inventory API used by the billing app.", "Reworded."))
    prose = await _record(session_factory, repository_id, repo, "c" * 40)
    assert prose.contract_snapshots_created == 0
    spec.write_text(spec.read_text().replace("scheme: bearer", "scheme: basic"))
    auth = await _record(session_factory, repository_id, repo, "d" * 40)
    assert auth.contract_snapshots_created == 1
    openapi = await _dependency(session_factory, repository_id, "openapi:openapi.yaml")
    async with session_factory() as session:
        spec_history = await DependencyRegistry().contract_history(session, dependency_id=openapi.id)
    assert len(spec_history) == 2 and spec_history[-1].fingerprint == openapi.contract_fingerprint
    stored = json.loads(spec_history[-1].normalized_contract or "{}")
    assert stored["security_schemes"]["bearerAuth"]["scheme"] == "basic"

    # 5. Removed dependency: kept for history, marked removed; reappearing reactivates it.
    shutil.rmtree(repo / "billing")
    (repo / "package.json").unlink()
    (repo / "package-lock.json").unlink()
    removed = await _record(session_factory, repository_id, repo, "e" * 40)
    assert removed.removed == 1
    stripe = await _dependency(session_factory, repository_id, "stripe:npm")
    assert stripe.status == STATUS_REMOVED and stripe.removed_at is not None
    async with session_factory() as session:
        active = await DependencyRegistry().list_dependencies(session, repository_id=repository_id)
    assert "stripe:npm" not in {d.dependency_key for d in active}

    shutil.copytree(MIXED / "billing", repo / "billing")
    shutil.copy(MIXED / "package.json", repo / "package.json")
    shutil.copy(MIXED / "package-lock.json", repo / "package-lock.json")
    back = await _record(session_factory, repository_id, repo, "f" * 40)
    assert back.reactivated == 1
    assert (await _dependency(session_factory, repository_id, "stripe:npm")).status == STATUS_ACTIVE

    # 6. Usage-site change rewrites that dependency's sites only.
    chat = repo / "app" / "ai" / "chat.py"
    chat.write_text(chat.read_text().replace("client.moderations.create(input=text)", "None"))
    changed = await _record(session_factory, repository_id, repo, "0" * 40)
    assert changed.usage_site_sets_rewritten == 1
    openai = await _dependency(session_factory, repository_id, "openai:pypi")
    async with session_factory() as session:
        tokens = {s.token for s in await DependencyRegistry().usage_sites(session, dependency_id=openai.id)}
    assert "moderations.create" not in tokens and "chat.completions.create" in tokens


async def test_registry_never_stores_secret_values(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    repo = tmp_path / "repo"
    shutil.copytree(MIXED, repo)
    fake = "PFTEST-REGISTRY-NOT-A-SECRET-77"
    (repo / ".env").write_text(f"OPENAI_API_KEY={fake}\n")
    repository_id = await _repository(session_factory)
    await _record(session_factory, repository_id, repo, "a" * 40)
    async with session_factory() as session:
        dumped = []
        for model in (ExternalDependencyModel, ExternalDependencyUsageSiteModel, ExternalContractSnapshotModel):
            for row in (await session.execute(select(model))).scalars().all():
                dumped.append(repr({c.name: getattr(row, c.name) for c in model.__table__.columns}))
    assert fake not in "\n".join(dumped)


async def test_deleting_a_repository_cascades_to_its_registry_on_postgres(tmp_path: Path) -> None:
    engine = await postgres_engine_or_skip("external_dependencies")
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        repository_id = await _repository(session_factory)
        await _record(session_factory, repository_id, MIXED, "a" * 40)
        async with session_factory() as session:
            dependency_ids = (await session.execute(select(ExternalDependencyModel.id).where(
                ExternalDependencyModel.repository_id == repository_id
            ))).scalars().all()
            assert dependency_ids
            await session.execute(text("DELETE FROM repositories WHERE id = :id"), {"id": repository_id})
            await session.commit()
        async with session_factory() as session:
            for model in (ExternalDependencyUsageSiteModel, ExternalContractSnapshotModel):
                remaining = (await session.execute(
                    select(func.count()).select_from(model).where(model.dependency_id.in_(dependency_ids))
                )).scalar_one()
                assert remaining == 0
            assert (await session.execute(select(func.count()).select_from(ExternalDependencyModel).where(
                ExternalDependencyModel.repository_id == repository_id
            ))).scalar_one() == 0
    finally:
        await engine.dispose()
