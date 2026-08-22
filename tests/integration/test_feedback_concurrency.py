"""Concurrent-feedback-ingestion regression test against a real
PostgreSQL database.

Mirrors ``tests/integration/test_review_run_concurrency.py`` exactly --
SQLite's SAVEPOINT emulation doesn't give the same guarantees under real
concurrent connections. Skips (rather than failing) if a migrated
Postgres isn't reachable at ``localhost:5432``.

Guards against: two concurrent sync attempts (e.g. two workers, or a
manual sync racing a scheduled one) both racing past the idempotency
check on the same raw event and persisting it twice.
``FeedbackEventRepository.create_if_new``'s ``SAVEPOINT``-scoped insert
plus ``uq_feedback_events_external_identity`` must guarantee only one row
ever survives.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import OperationalError, ProgrammingError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from patchfrog.feedback.domain import (
    ActorIdentity,
    FeedbackEvent,
    FeedbackEventType,
    FeedbackSource,
    NormalizedReactionHint,
    SignalStrength,
)
from patchfrog.persistence.models.feedback import FeedbackEventModel
from patchfrog.persistence.repositories import RepositoryRepository
from patchfrog.persistence.repositories.feedback import FeedbackEventRepository

_POSTGRES_URL = "postgresql+asyncpg://patchfrog:patchfrog@localhost:5432/patchfrog"


async def _postgres_available() -> AsyncEngine | None:
    engine = create_async_engine(_POSTGRES_URL)
    try:
        async with engine.begin() as conn:
            await conn.execute(text("SELECT 1 FROM feedback_events LIMIT 1"))
    except (OperationalError, ProgrammingError):
        await engine.dispose()
        return None
    return engine


async def test_two_concurrent_ingestions_of_the_same_raw_event_never_duplicate() -> None:
    engine = await _postgres_available()
    if engine is None:
        pytest.skip("real PostgreSQL not reachable at localhost:5432 (docker compose up -d postgres)")

    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    full_name = f"feedback-concurrency-test/{uuid.uuid4().hex[:8]}"

    try:
        async with session_factory() as session:
            repo_row = await RepositoryRepository().upsert(
                session,
                github_repository_id=uuid.uuid4().int & 0x7FFFFFFFFFFFFFFF,
                owner="feedback-concurrency-test",
                name=full_name,
                full_name=full_name,
                installation_id=0,
            )
            await session.commit()
            repository_id = repo_row.id

        external_event_id = f"reaction:{uuid.uuid4().hex}"

        def _make_event() -> FeedbackEvent:
            return FeedbackEvent(
                repository_id=repository_id, pull_request_id=None, review_run_id=None, publication_id=None,
                review_publication_comment_id=None, finding_id=None, github_review_id=None, github_comment_id=None,
                event_type=FeedbackEventType.REACTION_ADDED, source=FeedbackSource.REACTION_SYNC,
                external_event_id=external_event_id, raw_signal="+1",
                normalized_signal=NormalizedReactionHint.POSITIVE_HINT.value, signal_strength=SignalStrength.WEAK,
                actor=ActorIdentity(login="dev", is_bot=False), occurred_at=datetime.now(UTC), metadata={},
            )

        async def _ingest_in_own_session() -> FeedbackEventModel | None:
            async with session_factory() as session:
                model = await FeedbackEventRepository().create_if_new(session, event=_make_event())
                await session.commit()
                return model

        results = await asyncio.gather(_ingest_in_own_session(), _ingest_in_own_session())

        successes = [r for r in results if r is not None]
        assert len(successes) == 1  # exactly one of the two concurrent attempts wins

        async with session_factory() as session:
            rows = (
                await session.execute(
                    select(FeedbackEventModel).where(FeedbackEventModel.external_event_id == external_event_id)
                )
            ).scalars().all()
        assert len(rows) == 1
    finally:
        await engine.dispose()
