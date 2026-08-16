from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from patchfrog.persistence.models.analysis import AnalysisRunModel, AnalysisRunStatus


class AnalysisRunRepository:
    """Persistence operations for :class:`AnalysisRunModel`.

    Identity for idempotency/concurrency purposes is ``(repository_id,
    commit_sha, config_fingerprint, toolchain_fingerprint)`` — repeated
    requests with that same identity reuse an existing *succeeded* run
    rather than duplicating one. ``config_fingerprint`` is configuration
    *intent* (:meth:`patchfrog.analysis.config.AnalysisConfig.fingerprint`);
    ``toolchain_fingerprint`` is the *effective* toolchain actually
    discovered/used (:meth:`patchfrog.analysis.toolchain.ToolchainSnapshot.fingerprint`
    — analyzer versions, bundled ruleset content, engine version). Both
    must match for a prior run to be reused: identical config against a
    different analyzer version (or a changed bundled ruleset) is a
    different identity, since analyzer behavior/findings can change
    without the repository or config changing at all.

    The initial creation, the pre-write claim, and the final success
    transition are all guarded by a transaction-scoped PostgreSQL advisory
    lock keyed by that identity (the same pattern Phase 2's audit
    established for ``repository_indexes``): a concurrent duplicate
    request waits its turn instead of racing the partial unique index on
    ``status = 'succeeded'``. On SQLite (tests), this is a no-op — no real
    concurrency to protect against there.

    ``claim_for_write`` must run before any findings/executions are added
    to the persistence transaction — checking only at the final
    ``mark_succeeded`` transition is too late, since a losing run's
    already-queued finding rows would still be committed alongside its
    failure status.
    """

    async def _lock_identity(
        self,
        session: AsyncSession,
        *,
        repository_id: uuid.UUID,
        commit_sha: str,
        config_fingerprint: str,
        toolchain_fingerprint: str,
    ) -> None:
        if session.bind is None or session.bind.dialect.name != "postgresql":
            return
        key_material = f"{repository_id}:{commit_sha}:{config_fingerprint}:{toolchain_fingerprint}"
        digest = hashlib.sha256(key_material.encode()).digest()[:8]
        lock_key = int.from_bytes(digest, "big") & 0x7FFFFFFFFFFFFFFF
        await session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": lock_key})

    async def get_succeeded(
        self,
        session: AsyncSession,
        *,
        repository_id: uuid.UUID,
        commit_sha: str,
        config_fingerprint: str,
        toolchain_fingerprint: str,
    ) -> AnalysisRunModel | None:
        result = await session.execute(
            select(AnalysisRunModel).where(
                AnalysisRunModel.repository_id == repository_id,
                AnalysisRunModel.commit_sha == commit_sha,
                AnalysisRunModel.config_fingerprint == config_fingerprint,
                AnalysisRunModel.toolchain_fingerprint == toolchain_fingerprint,
                AnalysisRunModel.status == AnalysisRunStatus.SUCCEEDED,
            )
        )
        return result.scalar_one_or_none()

    async def claim_for_write(
        self,
        session: AsyncSession,
        *,
        run_id: uuid.UUID,
        repository_id: uuid.UUID,
        commit_sha: str,
        config_fingerprint: str,
        toolchain_fingerprint: str,
    ) -> AnalysisRunModel | None:
        """Acquire this identity's advisory lock and decide whether ``run_id``
        gets to persist its results.

        Must be called *before* any finding/execution rows are added to
        ``session`` for this run. Returns ``None`` if ``run_id`` has won the
        race and should proceed to persist (the lock is now held for the
        rest of this transaction, blocking any concurrent claimant until
        commit). Returns the existing succeeded run if a concurrent run
        already won — the caller must not persist any findings/executions
        in that case, only mark ``run_id`` failed as superseded, since
        anything else added to this transaction would still be committed
        alongside that failure.
        """

        await self._lock_identity(
            session,
            repository_id=repository_id,
            commit_sha=commit_sha,
            config_fingerprint=config_fingerprint,
            toolchain_fingerprint=toolchain_fingerprint,
        )
        existing = await self.get_succeeded(
            session,
            repository_id=repository_id,
            commit_sha=commit_sha,
            config_fingerprint=config_fingerprint,
            toolchain_fingerprint=toolchain_fingerprint,
        )
        if existing is not None and existing.id != run_id:
            return existing
        return None

    async def get_or_create_running(
        self,
        session: AsyncSession,
        *,
        repository_id: uuid.UUID,
        repository_index_id: uuid.UUID,
        commit_sha: str,
        config_fingerprint: str,
        toolchain_fingerprint: str,
        pull_request_id: uuid.UUID | None = None,
    ) -> tuple[AnalysisRunModel, bool]:
        """Return ``(run, is_new)``.

        If a succeeded run already exists for this identity, it's
        returned unchanged with ``is_new=False``. Otherwise a fresh
        ``RUNNING`` row is created and returned with ``is_new=True``.
        """

        await self._lock_identity(
            session,
            repository_id=repository_id,
            commit_sha=commit_sha,
            config_fingerprint=config_fingerprint,
            toolchain_fingerprint=toolchain_fingerprint,
        )
        existing = await self.get_succeeded(
            session,
            repository_id=repository_id,
            commit_sha=commit_sha,
            config_fingerprint=config_fingerprint,
            toolchain_fingerprint=toolchain_fingerprint,
        )
        if existing is not None:
            return existing, False

        model = AnalysisRunModel(
            repository_id=repository_id,
            repository_index_id=repository_index_id,
            pull_request_id=pull_request_id,
            commit_sha=commit_sha,
            config_fingerprint=config_fingerprint,
            toolchain_fingerprint=toolchain_fingerprint,
            status=AnalysisRunStatus.RUNNING,
            started_at=datetime.now(UTC),
        )
        session.add(model)
        await session.flush()
        return model, True

    async def mark_succeeded(
        self,
        session: AsyncSession,
        *,
        run_id: uuid.UUID,
        status: AnalysisRunStatus,
        analyzers_succeeded: int,
        analyzers_failed: int,
        analyzers_skipped: int,
        raw_findings_count: int,
        findings_count: int,
        duration_ms: float,
    ) -> AnalysisRunModel:
        """Mark a run succeeded or partial. Returns the *canonical* run for
        this identity — if a concurrent run already claimed
        ``status='succeeded'`` first, this run is marked ``failed`` as
        superseded and the winner is returned instead (see the class
        docstring)."""

        model = await session.get(AnalysisRunModel, run_id)
        if model is None:
            raise ValueError(f"No analysis run with id {run_id}")

        await self._lock_identity(
            session,
            repository_id=model.repository_id,
            commit_sha=model.commit_sha,
            config_fingerprint=model.config_fingerprint,
            toolchain_fingerprint=model.toolchain_fingerprint,
        )
        existing = await self.get_succeeded(
            session,
            repository_id=model.repository_id,
            commit_sha=model.commit_sha,
            config_fingerprint=model.config_fingerprint,
            toolchain_fingerprint=model.toolchain_fingerprint,
        )
        if existing is not None and existing.id != model.id:
            model.status = AnalysisRunStatus.FAILED
            model.error_message = f"superseded by concurrent analysis run {existing.id}"
            model.completed_at = datetime.now(UTC)
            await session.flush()
            return existing

        model.status = status
        model.analyzers_succeeded = analyzers_succeeded
        model.analyzers_failed = analyzers_failed
        model.analyzers_skipped = analyzers_skipped
        model.raw_findings_count = raw_findings_count
        model.findings_count = findings_count
        model.duration_ms = duration_ms
        model.completed_at = datetime.now(UTC)
        await session.flush()
        return model

    async def mark_failed(
        self, session: AsyncSession, *, run_id: uuid.UUID, error_message: str
    ) -> AnalysisRunModel:
        model = await session.get(AnalysisRunModel, run_id)
        if model is None:
            raise ValueError(f"No analysis run with id {run_id}")

        model.status = AnalysisRunStatus.FAILED
        model.error_message = error_message
        model.completed_at = datetime.now(UTC)
        await session.flush()
        return model

    async def get_by_id(self, session: AsyncSession, *, run_id: uuid.UUID) -> AnalysisRunModel | None:
        return await session.get(AnalysisRunModel, run_id)

    async def get_latest_succeeded_for_commit(
        self, session: AsyncSession, *, repository_id: uuid.UUID, commit_sha: str
    ) -> AnalysisRunModel | None:
        """The most recently completed succeeded run for this commit,
        regardless of which config/toolchain fingerprint produced it --
        used by the AI Reviewer (:mod:`patchfrog.review.service`) to find
        *any* static-analysis evidence for a commit without needing to
        recompute the exact fingerprints a caller isn't tracking."""

        result = await session.execute(
            select(AnalysisRunModel)
            .where(
                AnalysisRunModel.repository_id == repository_id,
                AnalysisRunModel.commit_sha == commit_sha,
                AnalysisRunModel.status == AnalysisRunStatus.SUCCEEDED,
            )
            .order_by(AnalysisRunModel.completed_at.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()
