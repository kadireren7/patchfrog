"""Shared prerequisite contract for tests that require real PostgreSQL.

Local deterministic runs skip these tests when the expected migrated test
database is absent or incompatible. CI sets ``PATCHFROG_REQUIRE_POSTGRES=1``
because it declares the service itself; under that contract an unavailable,
misconfigured, or unmigrated database is an infrastructure failure, not a
pass.
"""

from __future__ import annotations

import os

import pytest
from asyncpg import PostgresError  # type: ignore[import-untyped]
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

POSTGRES_TEST_URL = "postgresql+asyncpg://patchfrog:patchfrog@localhost:5432/patchfrog"


async def postgres_engine_or_skip(required_table: str) -> AsyncEngine:
    """Return a usable migrated Postgres engine or apply the explicit policy."""

    engine = create_async_engine(POSTGRES_TEST_URL)
    try:
        async with engine.begin() as conn:
            await conn.execute(text(f'SELECT 1 FROM "{required_table}" LIMIT 1'))
    # asyncpg authentication/startup failures can escape before SQLAlchemy
    # wraps them; both layers are part of this prerequisite boundary.
    except (SQLAlchemyError, PostgresError, OSError) as exc:
        await engine.dispose()
        detail = f"real PostgreSQL prerequisite unavailable or unmigrated ({type(exc).__name__})"
        if os.environ.get("PATCHFROG_REQUIRE_POSTGRES") == "1":
            raise RuntimeError(detail) from exc
        pytest.skip(f"{detail}; run `docker compose up -d postgres` and `alembic upgrade head`")
    return engine


__all__ = ["postgres_engine_or_skip"]
