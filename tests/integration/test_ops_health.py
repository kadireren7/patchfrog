"""Integration coverage for :mod:`patchfrog.ops.health` -- readiness must
fail closed on both a missing/mismatched migration and an unreachable
Redis, never report healthy on a guess.
"""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from patchfrog.ops.health import (
    check_database,
    check_readiness,
    check_redis,
    expected_migration_head,
)


async def test_missing_alembic_version_table_is_unhealthy(db_engine: AsyncEngine) -> None:
    check = await check_database(db_engine)
    assert check.healthy is False
    assert check.name == "database"


async def test_matching_migration_head_is_healthy(db_engine: AsyncEngine) -> None:
    expected = expected_migration_head()
    assert expected is not None, "the repo's own migration scripts must resolve a head"

    async with db_engine.begin() as conn:
        await conn.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32))"))
        await conn.execute(text("INSERT INTO alembic_version (version_num) VALUES (:v)"), {"v": expected})

    check = await check_database(db_engine)
    assert check.healthy is True
    assert expected in check.detail


async def test_mismatched_migration_head_is_unhealthy(db_engine: AsyncEngine) -> None:
    async with db_engine.begin() as conn:
        await conn.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32))"))
        await conn.execute(
            text("INSERT INTO alembic_version (version_num) VALUES (:v)"), {"v": "0000_stale_revision"}
        )

    check = await check_database(db_engine)
    assert check.healthy is False
    assert "migration mismatch" in check.detail


async def test_unreachable_redis_is_unhealthy() -> None:
    check = await check_redis("redis://127.0.0.1:1/0")
    assert check.healthy is False
    assert check.name == "redis"


async def test_readiness_report_is_unhealthy_if_any_check_fails(db_engine: AsyncEngine) -> None:
    report = await check_readiness(engine=db_engine, redis_url="redis://127.0.0.1:1/0")
    assert report.healthy is False
    assert len(report.checks) == 2
    assert {c.name for c in report.checks} == {"database", "redis"}
