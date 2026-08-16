from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from patchfrog.context.domain import ContextTargetType
from patchfrog.persistence.models.context import ContextBundleModel, ContextBundleStatus


class ContextBundleRepository:
    """Persistence operations for :class:`ContextBundleModel`.

    Identity for idempotency/concurrency purposes is ``(repository_id,
    commit_sha, target_fingerprint, config_fingerprint)`` -- mirrors
    :class:`patchfrog.persistence.repositories.analysis_run.AnalysisRunRepository`
    exactly: a transaction-scoped PostgreSQL advisory lock keyed by that
    identity guards creation, the pre-write claim, and the final success
    transition, so a losing concurrent request never leaves orphaned rows
    behind. On SQLite (tests), locking is a no-op -- no real concurrency
    to protect against there.
    """

    async def _lock_identity(
        self,
        session: AsyncSession,
        *,
        repository_id: uuid.UUID,
        commit_sha: str,
        target_fingerprint: str,
        config_fingerprint: str,
    ) -> None:
        if session.bind is None or session.bind.dialect.name != "postgresql":
            return
        key_material = f"context:{repository_id}:{commit_sha}:{target_fingerprint}:{config_fingerprint}"
        digest = hashlib.sha256(key_material.encode()).digest()[:8]
        lock_key = int.from_bytes(digest, "big") & 0x7FFFFFFFFFFFFFFF
        await session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": lock_key})

    async def get_succeeded(
        self,
        session: AsyncSession,
        *,
        repository_id: uuid.UUID,
        commit_sha: str,
        target_fingerprint: str,
        config_fingerprint: str,
    ) -> ContextBundleModel | None:
        result = await session.execute(
            select(ContextBundleModel).where(
                ContextBundleModel.repository_id == repository_id,
                ContextBundleModel.commit_sha == commit_sha,
                ContextBundleModel.target_fingerprint == target_fingerprint,
                ContextBundleModel.config_fingerprint == config_fingerprint,
                ContextBundleModel.status == ContextBundleStatus.SUCCEEDED,
            )
        )
        return result.scalar_one_or_none()

    async def claim_for_write(
        self,
        session: AsyncSession,
        *,
        bundle_id: uuid.UUID,
        repository_id: uuid.UUID,
        commit_sha: str,
        target_fingerprint: str,
        config_fingerprint: str,
    ) -> ContextBundleModel | None:
        """See ``AnalysisRunRepository.claim_for_write`` -- identical
        pattern. Must run before any item rows are added to ``session``."""

        await self._lock_identity(
            session,
            repository_id=repository_id,
            commit_sha=commit_sha,
            target_fingerprint=target_fingerprint,
            config_fingerprint=config_fingerprint,
        )
        existing = await self.get_succeeded(
            session,
            repository_id=repository_id,
            commit_sha=commit_sha,
            target_fingerprint=target_fingerprint,
            config_fingerprint=config_fingerprint,
        )
        if existing is not None and existing.id != bundle_id:
            return existing
        return None

    async def get_or_create_running(
        self,
        session: AsyncSession,
        *,
        repository_id: uuid.UUID,
        repository_index_id: uuid.UUID,
        commit_sha: str,
        target_fingerprint: str,
        config_fingerprint: str,
        engine_version: int,
        target_type: ContextTargetType,
        target_file_path: str,
        target_line: int | None,
        target_symbol_id: uuid.UUID | None,
        finding_id: uuid.UUID | None,
        analysis_run_id: uuid.UUID | None,
    ) -> tuple[ContextBundleModel, bool]:
        await self._lock_identity(
            session,
            repository_id=repository_id,
            commit_sha=commit_sha,
            target_fingerprint=target_fingerprint,
            config_fingerprint=config_fingerprint,
        )
        existing = await self.get_succeeded(
            session,
            repository_id=repository_id,
            commit_sha=commit_sha,
            target_fingerprint=target_fingerprint,
            config_fingerprint=config_fingerprint,
        )
        if existing is not None:
            return existing, False

        model = ContextBundleModel(
            repository_id=repository_id,
            repository_index_id=repository_index_id,
            commit_sha=commit_sha,
            target_fingerprint=target_fingerprint,
            config_fingerprint=config_fingerprint,
            engine_version=engine_version,
            target_type=target_type,
            target_file_path=target_file_path,
            target_line=target_line,
            target_symbol_id=target_symbol_id,
            finding_id=finding_id,
            analysis_run_id=analysis_run_id,
            status=ContextBundleStatus.RUNNING,
            started_at=datetime.now(UTC),
        )
        session.add(model)
        await session.flush()
        return model, True

    async def mark_succeeded(
        self,
        session: AsyncSession,
        *,
        bundle_id: uuid.UUID,
        candidate_count: int,
        selected_count: int,
        dropped_budget: int,
        dropped_overlap: int,
        dropped_duplicate: int,
        total_tokens: int,
        total_lines: int,
        generation_ms: float,
    ) -> ContextBundleModel:
        model = await session.get(ContextBundleModel, bundle_id)
        if model is None:
            raise ValueError(f"No context bundle with id {bundle_id}")

        await self._lock_identity(
            session,
            repository_id=model.repository_id,
            commit_sha=model.commit_sha,
            target_fingerprint=model.target_fingerprint,
            config_fingerprint=model.config_fingerprint,
        )
        existing = await self.get_succeeded(
            session,
            repository_id=model.repository_id,
            commit_sha=model.commit_sha,
            target_fingerprint=model.target_fingerprint,
            config_fingerprint=model.config_fingerprint,
        )
        if existing is not None and existing.id != model.id:
            model.status = ContextBundleStatus.FAILED
            model.error_message = f"superseded by concurrent context bundle {existing.id}"
            model.completed_at = datetime.now(UTC)
            await session.flush()
            return existing

        model.status = ContextBundleStatus.SUCCEEDED
        model.candidate_count = candidate_count
        model.selected_count = selected_count
        model.dropped_budget = dropped_budget
        model.dropped_overlap = dropped_overlap
        model.dropped_duplicate = dropped_duplicate
        model.total_tokens = total_tokens
        model.total_lines = total_lines
        model.generation_ms = generation_ms
        model.completed_at = datetime.now(UTC)
        await session.flush()
        return model

    async def mark_failed(
        self, session: AsyncSession, *, bundle_id: uuid.UUID, error_message: str
    ) -> ContextBundleModel:
        model = await session.get(ContextBundleModel, bundle_id)
        if model is None:
            raise ValueError(f"No context bundle with id {bundle_id}")
        model.status = ContextBundleStatus.FAILED
        model.error_message = error_message
        model.completed_at = datetime.now(UTC)
        await session.flush()
        return model

    async def get_by_id(self, session: AsyncSession, *, bundle_id: uuid.UUID) -> ContextBundleModel | None:
        return await session.get(ContextBundleModel, bundle_id)

    async def get_succeeded_for_finding(
        self, session: AsyncSession, *, finding_id: uuid.UUID
    ) -> ContextBundleModel | None:
        result = await session.execute(
            select(ContextBundleModel)
            .where(
                ContextBundleModel.finding_id == finding_id,
                ContextBundleModel.status == ContextBundleStatus.SUCCEEDED,
            )
            .order_by(ContextBundleModel.created_at.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def list_succeeded_for_target_symbol(
        self, session: AsyncSession, *, symbol_id: uuid.UUID
    ) -> list[ContextBundleModel]:
        result = await session.execute(
            select(ContextBundleModel)
            .where(
                ContextBundleModel.target_symbol_id == symbol_id,
                ContextBundleModel.status == ContextBundleStatus.SUCCEEDED,
            )
            .order_by(ContextBundleModel.created_at.desc())
        )
        return list(result.scalars().all())
