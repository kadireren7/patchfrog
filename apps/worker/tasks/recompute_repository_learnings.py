"""Celery task: background, idempotent, bounded recomputation of one
repository's durable learning records (Y12).

Deliberately **not** on the PR-review critical path -- triggered
post-review (see the publish task's own success path) or on a periodic
schedule an operator configures, never inline with
:mod:`patchfrog.review.service`'s own run. A single repository's
recomputation is bounded (see
:data:`patchfrog.learning_records.service.MAX_RECORDS_PER_RECOMPUTATION`)
and idempotent (re-running it twice in a row produces the same
persisted state, never a growing duplicate history) -- safe to retry,
safe to run on a schedule, never an unbounded async loop.
"""

from __future__ import annotations

import asyncio
import uuid

import structlog

from apps.worker.celery_app import celery_app
from patchfrog.config.settings import get_settings
from patchfrog.learning_records.service import recompute_repository_learnings
from patchfrog.persistence.database import create_engine, create_session_factory

logger = structlog.get_logger(__name__)


async def _recompute(repository_id: uuid.UUID) -> int:
    settings = get_settings()
    engine = create_engine(settings.database_url)
    try:
        session_factory = create_session_factory(engine)
        async with session_factory() as session:
            records = await recompute_repository_learnings(session, repository_id=repository_id)
            await session.commit()
        return len(records)
    finally:
        await engine.dispose()


@celery_app.task(name="patchfrog.recompute_repository_learnings")  # type: ignore[untyped-decorator]
def recompute_repository_learnings_task(*, repository_id: str) -> int:
    count = asyncio.run(_recompute(uuid.UUID(repository_id)))
    logger.info("repository_learnings_recomputed", repository_id=repository_id, record_count=count)
    return count
