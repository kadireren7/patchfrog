"""Database engine and session construction.

Callers (the API app, the worker, tests) are responsible for creating one
engine/session-factory pair at process startup and reusing it — this
module intentionally holds no global state.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)


def create_engine(database_url: str, *, echo: bool = False) -> AsyncEngine:
    """Create an async SQLAlchemy engine for ``database_url``."""

    return create_async_engine(database_url, echo=echo, pool_pre_ping=True)


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Create a session factory bound to ``engine``.

    ``expire_on_commit=False`` so ORM objects returned from a repository
    remain usable after the session that loaded them has committed.
    """

    return async_sessionmaker(engine, expire_on_commit=False)
