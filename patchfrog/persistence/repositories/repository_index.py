from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from patchfrog.persistence.models.repository_index import IndexStatus, RepositoryIndexModel


class RepositoryIndexRepository:
    """Persistence operations for :class:`RepositoryIndexModel`.

    ``is_active`` is only ever moved by :meth:`mark_succeeded` — a failed
    or still-running index never touches it, so the last succeeded index
    stays queryable as the "current" one regardless of what happens to
    any later run.

    Two indexing runs for the *same* repository can race — e.g. two
    Celery deliveries for the same push, or a manual re-trigger overlapping
    a scheduled one. Without serialization, concurrent
    ``SELECT MAX(index_version) ... ; INSERT`` and concurrent
    activate/deactivate flips can hit ``uq_repository_indexes_repo_version``
    or the partial unique ``is_active`` index. Rather than let either
    surface as a raw, unhandled ``IntegrityError``, every version-assigning
    or activation-flipping section takes a transaction-scoped PostgreSQL
    advisory lock keyed by ``repository_id`` first — the loser simply waits
    its turn instead of racing. (SQLite, used in tests, has no advisory
    locks and no real concurrency to protect against, so this is a no-op
    there.)
    """

    async def _lock_repository(self, session: AsyncSession, *, repository_id: uuid.UUID) -> None:
        if session.bind is None or session.bind.dialect.name != "postgresql":
            return
        # Advisory lock keys are signed 64-bit ints; a UUID is 128 bits, so
        # fold it down to the low 63 bits (kept non-negative — irrelevant
        # for correctness, just avoids driver bigint-range surprises).
        lock_key = repository_id.int & 0x7FFFFFFFFFFFFFFF
        await session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": lock_key})

    async def get_by_id(
        self, session: AsyncSession, *, index_id: uuid.UUID
    ) -> RepositoryIndexModel | None:
        return await session.get(RepositoryIndexModel, index_id)

    async def get_active(
        self, session: AsyncSession, *, repository_id: uuid.UUID
    ) -> RepositoryIndexModel | None:
        result = await session.execute(
            select(RepositoryIndexModel).where(
                RepositoryIndexModel.repository_id == repository_id,
                RepositoryIndexModel.is_active.is_(True),
            )
        )
        return result.scalar_one_or_none()

    async def create_running(
        self, session: AsyncSession, *, repository_id: uuid.UUID, commit_sha: str
    ) -> RepositoryIndexModel:
        await self._lock_repository(session, repository_id=repository_id)
        result = await session.execute(
            select(func.coalesce(func.max(RepositoryIndexModel.index_version), 0)).where(
                RepositoryIndexModel.repository_id == repository_id
            )
        )
        next_version = result.scalar_one() + 1

        model = RepositoryIndexModel(
            repository_id=repository_id,
            commit_sha=commit_sha,
            index_version=next_version,
            status=IndexStatus.RUNNING,
            is_active=False,
            started_at=datetime.now(UTC),
        )
        session.add(model)
        await session.flush()
        return model

    async def mark_succeeded(
        self,
        session: AsyncSession,
        *,
        index_id: uuid.UUID,
        files_total: int,
        files_parsed: int,
        files_failed: int,
        files_reused: int,
        symbols_extracted: int,
        edges_created: int,
        duration_ms: float,
    ) -> RepositoryIndexModel:
        model = await session.get(RepositoryIndexModel, index_id)
        if model is None:
            raise ValueError(f"No repository index with id {index_id}")

        await self._lock_repository(session, repository_id=model.repository_id)

        # Deactivating the old row and activating the new one must be two
        # separate flushes — the partial unique index on (repository_id)
        # WHERE is_active is checked per-statement, so flushing both
        # changes together risks the two UPDATEs landing in an order that
        # transiently has two active rows for the same repository.
        previous_active = await self.get_active(session, repository_id=model.repository_id)
        if previous_active is not None and previous_active.id != model.id:
            previous_active.is_active = False
            await session.flush()

        model.status = IndexStatus.SUCCEEDED
        model.is_active = True
        model.files_total = files_total
        model.files_parsed = files_parsed
        model.files_failed = files_failed
        model.files_reused = files_reused
        model.symbols_extracted = symbols_extracted
        model.edges_created = edges_created
        model.duration_ms = duration_ms
        model.completed_at = datetime.now(UTC)
        await session.flush()
        return model

    async def mark_failed(
        self, session: AsyncSession, *, index_id: uuid.UUID, error_message: str
    ) -> RepositoryIndexModel:
        model = await session.get(RepositoryIndexModel, index_id)
        if model is None:
            raise ValueError(f"No repository index with id {index_id}")

        model.status = IndexStatus.FAILED
        model.error_message = error_message
        model.completed_at = datetime.now(UTC)
        await session.flush()
        return model
