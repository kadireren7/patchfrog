"""Idempotent persistence for migration PR publication (M9.1/M9.6).

Identity is :meth:`patchfrog.migration_pr.domain.MigrationPRLinkage.identity_key`
(repository + upstream change fingerprint + engine version) -- mirrors
:mod:`patchfrog.migration.store`'s advisory-lock/fingerprint-keyed-upsert
pattern, but keyed on the *migration's* identity rather than the patch's,
since the whole point is reconciling the same row across a regenerated
patch (M9.6), never creating a duplicate.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from patchfrog.migration_pr.domain import (
    MigrationPRLinkage,
    MigrationPRPlan,
    MigrationPRStatus,
    MigrationPullRequest,
)
from patchfrog.persistence.models.migration_pr import MigrationPRModel


async def _advisory_lock(session: AsyncSession, key: str) -> None:
    if session.bind is None or session.bind.dialect.name != "postgresql":
        return
    digest = hashlib.sha256(key.encode()).digest()[:8]
    await session.execute(
        text("SELECT pg_advisory_xact_lock(:key)"), {"key": int.from_bytes(digest, "big") & 0x7FFFFFFFFFFFFFFF}
    )


class MigrationPRStore:
    async def get(self, session: AsyncSession, *, identity_key: str) -> MigrationPRModel | None:
        return (
            await session.execute(select(MigrationPRModel).where(MigrationPRModel.identity_key == identity_key))
        ).scalar_one_or_none()

    async def upsert(
        self,
        session: AsyncSession,
        *,
        plan: MigrationPRPlan,
        result: MigrationPullRequest,
        recorded_at: datetime | None = None,
    ) -> tuple[MigrationPRModel, bool]:
        """Returns (row, created). One row per identity -- a re-run for
        the same upstream change always updates the same row (M9.6),
        recording whatever the latest patch/evidence/PR state is."""

        linkage = plan.linkage
        identity_key = linkage.identity_key()
        now = recorded_at or datetime.now(UTC)
        await _advisory_lock(session, f"migration-pr:{identity_key}")

        existing = await self.get(session, identity_key=identity_key)
        if existing is not None:
            existing.branch_name = plan.branch_name
            existing.patch_fingerprint = linkage.patch_fingerprint
            existing.verification_plan_fingerprint = linkage.verification_plan_fingerprint
            existing.bundle_fingerprint = linkage.bundle_fingerprint
            existing.base_commit_sha = linkage.base_commit_sha
            existing.eligibility = plan.eligibility.value
            existing.status = result.status.value
            existing.pr_number = result.number
            existing.pr_html_url = result.html_url
            existing.reason = result.reason
            existing.last_reconciled_at = now
            await session.flush()
            return existing, False

        row = MigrationPRModel(
            identity_key=identity_key, repository=linkage.repository, change_fingerprint=linkage.change_fingerprint,
            engine_version=linkage.engine_version, branch_name=plan.branch_name,
            patch_fingerprint=linkage.patch_fingerprint,
            verification_plan_fingerprint=linkage.verification_plan_fingerprint,
            bundle_fingerprint=linkage.bundle_fingerprint, base_commit_sha=linkage.base_commit_sha,
            eligibility=plan.eligibility.value, status=result.status.value, pr_number=result.number,
            pr_html_url=result.html_url, reason=result.reason, created_at=now, last_reconciled_at=now,
        )
        session.add(row)
        await session.flush()
        return row, True

    def to_domain(self, row: MigrationPRModel) -> MigrationPullRequest:
        return MigrationPullRequest(
            id=str(row.id), number=row.pr_number, html_url=row.pr_html_url, status=MigrationPRStatus(row.status),
            linkage=MigrationPRLinkage(
                change_fingerprint=row.change_fingerprint, dependency_key=None, repository=row.repository,
                base_commit_sha=row.base_commit_sha, plan_fingerprint="", patch_fingerprint=row.patch_fingerprint,
                verification_plan_fingerprint=row.verification_plan_fingerprint,
                bundle_fingerprint=row.bundle_fingerprint, engine_version=row.engine_version,
            ),
            branch_name=row.branch_name, reason=row.reason,
        )


__all__ = ["MigrationPRStore"]
