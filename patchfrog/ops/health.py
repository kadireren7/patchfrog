"""Readiness check logic -- kept out of the FastAPI route itself so it's
independently testable.

Liveness (``GET /health/live``) deliberately never touches the database
or Redis -- "is the process alive" must never depend on an external
system (spec section 5: "Do not require external providers or GitHub to
be healthy for API readiness unless absolutely necessary"). Readiness
(``GET /health/ready``) checks exactly the dependencies a request
actually needs to succeed: the database (reachable *and* on the
migration the running code expects -- a stale/ahead schema is reported
as unready, never silently ignored) and Redis (the Celery broker/result
backend). GitHub and the LLM provider are deliberately never checked
here -- their outages don't make the API itself unable to accept a
webhook.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import redis.asyncio as redis
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

_ALEMBIC_INI_PATH = Path(__file__).resolve().parents[2] / "alembic.ini"


@dataclass(frozen=True, slots=True)
class ReadinessCheck:
    name: str
    healthy: bool
    detail: str = ""


@dataclass(frozen=True, slots=True)
class ReadinessReport:
    healthy: bool
    checks: tuple[ReadinessCheck, ...]


def expected_migration_head() -> str | None:
    """The migration revision the *running code* expects to find applied
    -- derived from the committed migration scripts, never hardcoded, so
    this can never drift from reality on its own."""

    if not _ALEMBIC_INI_PATH.is_file():
        return None
    config = Config(str(_ALEMBIC_INI_PATH))
    script = ScriptDirectory.from_config(config)
    return script.get_current_head()


async def check_database(engine: AsyncEngine) -> ReadinessCheck:
    try:
        async with engine.connect() as conn:
            result = await conn.execute(text("SELECT version_num FROM alembic_version"))
            row = result.first()
            current = row[0] if row is not None else None
    except Exception as exc:
        return ReadinessCheck(name="database", healthy=False, detail=str(exc))

    expected = expected_migration_head()
    if expected is not None and current != expected:
        return ReadinessCheck(
            name="database",
            healthy=False,
            detail=f"migration mismatch: database is at {current!r}, running code expects {expected!r}",
        )
    return ReadinessCheck(name="database", healthy=True, detail=f"migration={current}")


async def check_redis(redis_url: str) -> ReadinessCheck:
    client: redis.Redis[bytes] = redis.from_url(redis_url, socket_connect_timeout=2, socket_timeout=2)
    try:
        await client.ping()
        return ReadinessCheck(name="redis", healthy=True)
    except Exception as exc:
        return ReadinessCheck(name="redis", healthy=False, detail=str(exc))
    finally:
        await client.close()


async def check_readiness(*, engine: AsyncEngine, redis_url: str) -> ReadinessReport:
    checks = (await check_database(engine), await check_redis(redis_url))
    return ReadinessReport(healthy=all(c.healthy for c in checks), checks=checks)
