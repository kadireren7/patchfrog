"""M10.9 on real PostgreSQL: concurrent upserts of one campaign identity never create a second campaign and
never lose a repository record (SQLite cannot reproduce the race: no real MVCC, no advisory locks).

Skips locally when a migrated Postgres is not reachable; CI sets ``PATCHFROG_REQUIRE_POSTGRES=1``."""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import replace

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from patchfrog.campaigns.domain import (
    CampaignState,
    CompatibilityCampaign,
    RepositoryRecord,
    RepoState,
)
from patchfrog.campaigns.orchestrate import run_campaign
from patchfrog.campaigns.policy import WatchMode, WorkspacePolicy
from patchfrog.campaigns.store import CampaignStore
from patchfrog.persistence.models.campaign import (
    CampaignRepositoryRecordModel,
    CompatibilityCampaignModel,
)
from tests.support.campaigns import NOW, acme_event, acme_input
from tests.support.postgres import postgres_engine_or_skip


async def _campaign(workspace_key: str) -> CompatibilityCampaign:
    event, hints = acme_event()
    run = await run_campaign(
        workspace_key=workspace_key, event=event, entries=[acme_input("acme-web"), acme_input("acme-search")], now=NOW,
        policy=WorkspacePolicy(watch_mode=WatchMode.DETECT_ONLY), hints=hints,
    )
    return run.campaign


async def test_concurrent_upserts_of_one_campaign_identity_yield_one_row() -> None:
    engine = await postgres_engine_or_skip("compatibility_campaigns")
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    workspace_key = f"pg-campaign-test-{uuid.uuid4().hex[:8]}"
    campaign = await _campaign(workspace_key)
    store = CampaignStore()

    async def upsert(variant: CompatibilityCampaign) -> bool:
        async with session_factory() as session:
            _, created = await store.upsert(session, variant, recorded_at=NOW)
            await session.commit()
            return created

    try:
        results = await asyncio.gather(*(upsert(campaign) for _ in range(8)))
        assert results.count(True) == 1  # exactly one creator; the advisory lock + unique identity held
        async with session_factory() as session:
            assert (
                await session.execute(
                    select(func.count()).select_from(CompatibilityCampaignModel).where(
                        CompatibilityCampaignModel.workspace_key == workspace_key
                    )
                )
            ).scalar_one() == 1
            loaded = await store.load(session, identity_key=campaign.campaign_id)
        assert loaded is not None and sorted(loaded.repositories_in_scope) == sorted(campaign.repositories_in_scope)
        # concurrent *different* states converge to the highest version, with one record row per repository
        bumped = replace(campaign, version=campaign.version + 1, state=CampaignState.ACTION_REQUIRED)
        await asyncio.gather(upsert(campaign), upsert(bumped), upsert(bumped))
        async with session_factory() as session:
            row = await store.get_row(session, identity_key=campaign.campaign_id)
            assert row is not None
            records = (
                await session.execute(
                    select(func.count()).select_from(CampaignRepositoryRecordModel).where(
                        CampaignRepositoryRecordModel.campaign_id == row.id
                    )
                )
            ).scalar_one()
        assert row.version == campaign.version + 1
        assert records == len(campaign.records)
    finally:
        async with session_factory() as session:
            await session.execute(
                delete(CompatibilityCampaignModel).where(CompatibilityCampaignModel.workspace_key == workspace_key)
            )
            await session.commit()
        await engine.dispose()


async def test_a_resolved_repository_state_round_trips_on_postgres() -> None:
    engine = await postgres_engine_or_skip("compatibility_campaigns")
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    workspace_key = f"pg-campaign-test-{uuid.uuid4().hex[:8]}"
    campaign = await _campaign(workspace_key)
    resolved: tuple[RepositoryRecord, ...] = tuple(replace(r, state=RepoState.RESOLVED, ever_affected=True) for r in campaign.records)
    campaign = replace(campaign, records=resolved, state=CampaignState.RESOLVED)
    store = CampaignStore()
    try:
        async with session_factory() as session:
            await store.upsert(session, campaign, recorded_at=NOW)
            await session.commit()
        async with session_factory() as session:
            loaded = await store.load(session, identity_key=campaign.campaign_id)
        assert loaded is not None and loaded.state is CampaignState.RESOLVED
        assert all(r.state is RepoState.RESOLVED and r.ever_affected for r in loaded.records)
    finally:
        async with session_factory() as session:
            await session.execute(
                delete(CompatibilityCampaignModel).where(CompatibilityCampaignModel.workspace_key == workspace_key)
            )
            await session.commit()
        await engine.dispose()
