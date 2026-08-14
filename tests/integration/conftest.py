from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool

from patchfrog.persistence.models import Base


@pytest.fixture
async def db_engine() -> AsyncIterator[AsyncEngine]:
    """A fresh in-memory SQLite database, schema created, per test.

    ``StaticPool`` keeps a single connection alive for the engine's
    lifetime — required for SQLite's ``:memory:`` database, which
    otherwise disappears between connections.
    """

    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    yield engine

    await engine.dispose()


@pytest.fixture
def session_factory(db_engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(db_engine, expire_on_commit=False)
