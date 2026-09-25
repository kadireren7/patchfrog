"""Persistent, idempotent external dependency + contract registry (M5.5).

``DependencyRegistry.record_inventory`` folds one
:class:`~patchfrog.dependencies.domain.DependencyInventory` into the
registry for a repository:

- new dependency -> one ``external_dependencies`` row + its usage sites +
  its first contract snapshot;
- identical re-discovery -> no new rows at all; only ``last_observed_*``
  moves (usage sites are rewritten only when their fingerprint changes);
- changed version/contract -> the dependency row is updated in place and
  a *new* contract snapshot is added (history kept, never overwritten);
- dependency no longer observed -> ``status='removed'`` (kept for
  history); observed again -> reactivated.

Writes for one repository are serialized with a transaction-scoped
PostgreSQL advisory lock (a no-op on SQLite), the same pattern every
other identity-keyed PatchFrog table uses.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from patchfrog.dependencies.domain import DependencyInventory, ExternalDependency
from patchfrog.persistence.models.dependency import (
    ExternalContractSnapshotModel,
    ExternalDependencyModel,
    ExternalDependencyUsageSiteModel,
)

#: A normalized contract larger than this is stored as fingerprint +
#: summary only (the fingerprint still covers the full structure).
MAX_NORMALIZED_CONTRACT_BYTES = 256_000

STATUS_ACTIVE = "active"
STATUS_REMOVED = "removed"


@dataclass(frozen=True, slots=True)
class RegistryWriteResult:
    created: int = 0
    updated: int = 0
    unchanged: int = 0
    removed: int = 0
    reactivated: int = 0
    contract_snapshots_created: int = 0
    usage_site_sets_rewritten: int = 0


def usage_fingerprint(dependency: ExternalDependency) -> str:
    payload = sorted(
        (s.file_path, s.line or 0, s.symbol or "", s.evidence_type.value, s.token, s.confidence.value)
        for s in dependency.usage_sites
    )
    return hashlib.sha256(json.dumps(payload, separators=(",", ":")).encode()).hexdigest()


def _normalized_json(dependency: ExternalDependency) -> str | None:
    if dependency.contract is None:
        return None
    rendered = json.dumps(dependency.contract.normalized, sort_keys=True, separators=(",", ":"), default=str)
    return rendered if len(rendered.encode()) <= MAX_NORMALIZED_CONTRACT_BYTES else None


class DependencyRegistry:
    async def _lock(self, session: AsyncSession, repository_id: uuid.UUID) -> None:
        if session.bind is None or session.bind.dialect.name != "postgresql":
            return
        digest = hashlib.sha256(f"dependency-registry:{repository_id}".encode()).digest()[:8]
        await session.execute(
            text("SELECT pg_advisory_xact_lock(:key)"), {"key": int.from_bytes(digest, "big") & 0x7FFFFFFFFFFFFFFF}
        )

    async def record_inventory(
        self,
        session: AsyncSession,
        *,
        repository_id: uuid.UUID,
        inventory: DependencyInventory,
        observed_at: datetime | None = None,
    ) -> RegistryWriteResult:
        now = observed_at or datetime.now(UTC)
        await self._lock(session, repository_id)
        rows = (
            await session.execute(
                select(ExternalDependencyModel).where(ExternalDependencyModel.repository_id == repository_id)
            )
        ).scalars().all()
        existing = {row.dependency_key: row for row in rows}
        counts = dict.fromkeys(
            ("created", "updated", "unchanged", "removed", "reactivated", "contract_snapshots_created",
             "usage_site_sets_rewritten"),
            0,
        )

        for dependency in inventory.dependencies:
            row = existing.get(dependency.key)
            fingerprint = usage_fingerprint(dependency)
            fields = self._fields(dependency, fingerprint, inventory)
            if row is None:
                row = ExternalDependencyModel(
                    repository_id=repository_id,
                    dependency_key=dependency.key,
                    status=STATUS_ACTIVE,
                    first_observed_at=now,
                    last_observed_at=now,
                    last_observed_commit_sha=inventory.commit_sha,
                    **fields,
                )
                session.add(row)
                await session.flush()
                self._add_sites(session, row.id, dependency)
                counts["created"] += 1
            else:
                changed = any(getattr(row, name) != value for name, value in fields.items())
                if row.status != STATUS_ACTIVE:
                    counts["reactivated"] += 1
                    row.status = STATUS_ACTIVE
                    row.removed_at = None
                elif changed:
                    counts["updated"] += 1
                else:
                    counts["unchanged"] += 1
                if row.usage_fingerprint != fingerprint:
                    await session.execute(
                        delete(ExternalDependencyUsageSiteModel).where(
                            ExternalDependencyUsageSiteModel.dependency_id == row.id
                        )
                    )
                    self._add_sites(session, row.id, dependency)
                    counts["usage_site_sets_rewritten"] += 1
                for name, value in fields.items():
                    setattr(row, name, value)
                row.last_observed_at = now
                row.last_observed_commit_sha = inventory.commit_sha
            if await self._record_contract(session, row.id, dependency, inventory, now):
                counts["contract_snapshots_created"] += 1

        observed_keys = {d.key for d in inventory.dependencies}
        for key, row in existing.items():
            if key not in observed_keys and row.status == STATUS_ACTIVE:
                row.status = STATUS_REMOVED
                row.removed_at = now
                counts["removed"] += 1
        await session.flush()
        return RegistryWriteResult(**counts)

    @staticmethod
    def _fields(dependency: ExternalDependency, fingerprint: str, inventory: DependencyInventory) -> dict[str, object]:
        contract = dependency.contract
        return {
            "provider_key": dependency.provider.key[:255],
            "display_name": dependency.provider.display_name[:255],
            "kind": dependency.kind.value,
            "ecosystem": dependency.ecosystem.value,
            "package_name": dependency.package_name,
            "declared_version": dependency.version.declared,
            "resolved_version": dependency.version.resolved,
            "confidence": dependency.confidence.value,
            "contract_source_type": contract.source.type.value if contract else None,
            "contract_source_ref": contract.source.ref[:1024] if contract else None,
            "contract_fingerprint": contract.fingerprint.value if contract else None,
            "usage_site_count": len(dependency.usage_sites),
            "usage_fingerprint": fingerprint,
            "evidence_counts": json.dumps(dependency.evidence_counts(), sort_keys=True),
            "metadata_json": json.dumps(dict(dependency.metadata), sort_keys=True),
            "discovery_version": inventory.discovery_version,
        }

    @staticmethod
    def _add_sites(session: AsyncSession, dependency_id: uuid.UUID, dependency: ExternalDependency) -> None:
        for site in dependency.usage_sites:
            session.add(
                ExternalDependencyUsageSiteModel(
                    dependency_id=dependency_id,
                    file_path=site.file_path[:1024],
                    line=site.line or 0,
                    symbol=site.symbol[:1024] if site.symbol else None,
                    evidence_type=site.evidence_type.value,
                    token=site.token[:512],
                    confidence=site.confidence.value,
                )
            )

    @staticmethod
    async def _record_contract(
        session: AsyncSession,
        dependency_id: uuid.UUID,
        dependency: ExternalDependency,
        inventory: DependencyInventory,
        now: datetime,
    ) -> bool:
        contract = dependency.contract
        if contract is None:
            return False
        snapshot = (
            await session.execute(
                select(ExternalContractSnapshotModel).where(
                    ExternalContractSnapshotModel.dependency_id == dependency_id,
                    ExternalContractSnapshotModel.fingerprint == contract.fingerprint.value,
                    ExternalContractSnapshotModel.normalization_version == contract.fingerprint.normalization_version,
                )
            )
        ).scalar_one_or_none()
        if snapshot is not None:
            snapshot.last_observed_at = now
            snapshot.last_observed_commit_sha = inventory.commit_sha
            return False
        session.add(
            ExternalContractSnapshotModel(
                dependency_id=dependency_id,
                fingerprint=contract.fingerprint.value,
                algorithm=contract.fingerprint.algorithm,
                normalization_version=contract.fingerprint.normalization_version,
                source_type=contract.source.type.value,
                source_ref=contract.source.ref[:1024],
                declared_version=dependency.version.declared,
                resolved_version=dependency.version.resolved,
                normalized_contract=_normalized_json(dependency),
                summary_json=json.dumps(dict(contract.summary), sort_keys=True),
                first_observed_at=now,
                last_observed_at=now,
                first_observed_commit_sha=inventory.commit_sha,
                last_observed_commit_sha=inventory.commit_sha,
            )
        )
        return True

    async def list_dependencies(
        self, session: AsyncSession, *, repository_id: uuid.UUID, include_removed: bool = False
    ) -> list[ExternalDependencyModel]:
        query = select(ExternalDependencyModel).where(ExternalDependencyModel.repository_id == repository_id)
        if not include_removed:
            query = query.where(ExternalDependencyModel.status == STATUS_ACTIVE)
        return list((await session.execute(query.order_by(ExternalDependencyModel.dependency_key))).scalars().all())

    async def usage_sites(
        self, session: AsyncSession, *, dependency_id: uuid.UUID
    ) -> list[ExternalDependencyUsageSiteModel]:
        query = (
            select(ExternalDependencyUsageSiteModel)
            .where(ExternalDependencyUsageSiteModel.dependency_id == dependency_id)
            .order_by(ExternalDependencyUsageSiteModel.file_path, ExternalDependencyUsageSiteModel.line)
        )
        return list((await session.execute(query)).scalars().all())

    async def contract_history(
        self, session: AsyncSession, *, dependency_id: uuid.UUID
    ) -> list[ExternalContractSnapshotModel]:
        query = (
            select(ExternalContractSnapshotModel)
            .where(ExternalContractSnapshotModel.dependency_id == dependency_id)
            .order_by(ExternalContractSnapshotModel.first_observed_at, ExternalContractSnapshotModel.fingerprint)
        )
        return list((await session.execute(query)).scalars().all())


__all__ = [
    "MAX_NORMALIZED_CONTRACT_BYTES",
    "STATUS_ACTIVE",
    "STATUS_REMOVED",
    "DependencyRegistry",
    "RegistryWriteResult",
    "usage_fingerprint",
]
