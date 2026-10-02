"""Idempotent persistence of migration verification evidence bundles (M8.9).

Mirrors :mod:`patchfrog.migration.store`'s pattern exactly: one row per
(patch, verification-plan fingerprint); re-verifying the identical patch
against the identical requirement/step derivation only refreshes
``last_verified_at``. A different patch fingerprint or a different
verification-plan fingerprint (the M8 engine's own logic changed) never
matches an existing row -- old evidence is invalidated by construction,
never silently reused for a new patch (M9.8's integrity requirement
starts here).
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from patchfrog.migration_verification.domain import MigrationEvidenceBundle
from patchfrog.migration_verification.report import bundle_to_dict
from patchfrog.persistence.models.migration_verification import MigrationVerificationRunModel

MAX_JSON_COLUMN_BYTES = 256_000


def _bounded(value: object, limit: int = MAX_JSON_COLUMN_BYTES) -> str:
    rendered = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    if len(rendered.encode()) <= limit:
        return rendered
    return json.dumps({"truncated": True, "bytes": len(rendered.encode())})


async def _advisory_lock(session: AsyncSession, key: str) -> None:
    if session.bind is None or session.bind.dialect.name != "postgresql":
        return
    digest = hashlib.sha256(key.encode()).digest()[:8]
    await session.execute(
        text("SELECT pg_advisory_xact_lock(:key)"), {"key": int.from_bytes(digest, "big") & 0x7FFFFFFFFFFFFFFF}
    )


class MigrationVerificationStore:
    async def record_run(
        self,
        session: AsyncSession,
        *,
        patch_id: uuid.UUID,
        bundle: MigrationEvidenceBundle,
        recorded_at: datetime | None = None,
    ) -> tuple[MigrationVerificationRunModel, bool]:
        """Returns (row, created)."""

        now = recorded_at or datetime.now(UTC)
        plan_fingerprint = bundle.plan.fingerprint()
        await _advisory_lock(session, f"migration-verification:{patch_id}:{plan_fingerprint}")
        existing = (
            await session.execute(
                select(MigrationVerificationRunModel).where(
                    MigrationVerificationRunModel.patch_id == patch_id,
                    MigrationVerificationRunModel.verification_plan_fingerprint == plan_fingerprint,
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            existing.last_verified_at = now
            existing.outcome = bundle.outcome.value
            existing.evidence_strength = bundle.evidence_strength.value
            existing.residual_risk = bundle.residual_risk.value
            existing.bundle_json = _bounded(bundle_to_dict(bundle))
            await session.flush()
            return existing, False

        row = MigrationVerificationRunModel(
            patch_id=patch_id,
            change_fingerprint=bundle.change_fingerprint,
            patch_fingerprint=bundle.patch_fingerprint,
            verification_plan_fingerprint=plan_fingerprint,
            engine_version=bundle.version,
            repository_head_sha=bundle.repository_head_sha,
            outcome=bundle.outcome.value,
            evidence_strength=bundle.evidence_strength.value,
            residual_risk=bundle.residual_risk.value,
            bundle_json=_bounded(bundle_to_dict(bundle)),
            created_at=now,
            last_verified_at=now,
        )
        session.add(row)
        await session.flush()
        return row, True

    async def latest_for_patch(
        self, session: AsyncSession, *, patch_id: uuid.UUID
    ) -> MigrationVerificationRunModel | None:
        query = (
            select(MigrationVerificationRunModel)
            .where(MigrationVerificationRunModel.patch_id == patch_id)
            .order_by(MigrationVerificationRunModel.last_verified_at.desc())
            .limit(1)
        )
        return (await session.execute(query)).scalar_one_or_none()


__all__ = ["MAX_JSON_COLUMN_BYTES", "MigrationVerificationStore"]
