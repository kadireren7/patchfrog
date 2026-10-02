"""Idempotent persistence for compatibility campaigns (M10.9).

One campaign row per identity key (workspace + upstream change + campaign
engine version); one record row per repository per campaign. ``upsert`` is
safe to call repeatedly with the same campaign: it reconciles in place, so
re-detecting a change never creates a second campaign. Writes for one
identity are serialized with a transaction-scoped advisory lock (a no-op on
SQLite), the same pattern every other identity-keyed table here uses.

Scope semantics: a campaign's records are exactly the repositories passed to
the latest run (the currently enrolled set). A repository that is no longer
enrolled is removed from the campaign; one that is enrolled but unreadable
stays, as ``ACCESS_LOST``.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime

from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from patchfrog.campaigns.domain import (
    CampaignState,
    CompatibilityCampaign,
    Freshness,
    OrgBlastRadius,
    OrgClass,
    RepositoryRecord,
    RepoState,
    UnknownReason,
)
from patchfrog.persistence.models.campaign import (
    CampaignRepositoryRecordModel,
    CompatibilityCampaignModel,
)


async def _advisory_lock(session: AsyncSession, key: str) -> None:
    if session.bind is None or session.bind.dialect.name != "postgresql":
        return
    digest = hashlib.sha256(key.encode()).digest()[:8]
    await session.execute(
        text("SELECT pg_advisory_xact_lock(:key)"), {"key": int.from_bytes(digest, "big") & 0x7FFFFFFFFFFFFFFF}
    )


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _blast_json(radius: OrgBlastRadius) -> str:
    return json.dumps(
        {
            "total_enrolled": radius.total_enrolled, "potentially_relevant": radius.potentially_relevant,
            "affected": list(radius.affected), "not_affected": list(radius.not_affected),
            "unknown": list(radius.unknown), "unknown_reasons": dict(radius.unknown_reasons),
        },
        sort_keys=True,
    )


def _blast_from_json(raw: str) -> OrgBlastRadius:
    data = json.loads(raw)
    return OrgBlastRadius(
        total_enrolled=int(data["total_enrolled"]), potentially_relevant=int(data["potentially_relevant"]),
        affected=tuple(data["affected"]), not_affected=tuple(data["not_affected"]), unknown=tuple(data["unknown"]),
        unknown_reasons=dict(data.get("unknown_reasons", {})),
    )


def _fill_record(row: CampaignRepositoryRecordModel, record: RepositoryRecord) -> None:
    row.state = record.state.value
    row.org_class = record.org_class.value
    row.freshness = record.freshness.value
    row.unknown_reason = record.unknown_reason.value if record.unknown_reason else None
    row.impact_status = record.impact_status
    row.direct_consumers = record.direct_consumers
    row.transitive_consumers = record.transitive_consumers
    row.potential_consumers = record.potential_consumers
    row.severity = record.severity
    row.migration_status = record.migration_status
    row.migration_strategy_available = record.migration_strategy_available
    row.verification_feasibility = record.verification_feasibility
    row.verification_outcome = record.verification_outcome
    row.publication_readiness = record.publication_readiness
    row.base_commit_sha = record.base_commit_sha
    row.plan_fingerprint = record.plan_fingerprint
    row.patch_fingerprint = record.patch_fingerprint
    row.bundle_fingerprint = record.bundle_fingerprint
    row.pr_status = record.pr_status
    row.pr_number = record.pr_number
    row.pr_url = record.pr_url
    row.residual_risk = record.residual_risk
    row.ever_affected = record.ever_affected
    row.reasons_json = json.dumps(list(record.reasons))
    row.human_actions_json = json.dumps(list(record.human_actions))
    row.error = record.error[:512] if record.error else None
    row.evaluated_at = record.evaluated_at


def _record_from_row(row: CampaignRepositoryRecordModel) -> RepositoryRecord:
    return RepositoryRecord(
        repository=row.repository, state=RepoState(row.state), org_class=OrgClass(row.org_class),
        freshness=Freshness(row.freshness), unknown_reason=UnknownReason(row.unknown_reason) if row.unknown_reason else None,
        impact_status=row.impact_status, direct_consumers=row.direct_consumers,
        transitive_consumers=row.transitive_consumers, potential_consumers=row.potential_consumers,
        severity=row.severity, migration_status=row.migration_status,
        migration_strategy_available=row.migration_strategy_available,
        verification_feasibility=row.verification_feasibility, verification_outcome=row.verification_outcome,
        publication_readiness=row.publication_readiness, base_commit_sha=row.base_commit_sha,
        plan_fingerprint=row.plan_fingerprint, patch_fingerprint=row.patch_fingerprint,
        bundle_fingerprint=row.bundle_fingerprint, pr_status=row.pr_status, pr_number=row.pr_number,
        pr_url=row.pr_url, residual_risk=row.residual_risk, ever_affected=row.ever_affected,
        reasons=tuple(json.loads(row.reasons_json)), human_actions=tuple(json.loads(row.human_actions_json)),
        error=row.error, evaluated_at=_aware(row.evaluated_at),
    )


class CampaignStore:
    async def get_row(self, session: AsyncSession, *, identity_key: str) -> CompatibilityCampaignModel | None:
        return (
            await session.execute(
                select(CompatibilityCampaignModel).where(CompatibilityCampaignModel.identity_key == identity_key)
            )
        ).scalar_one_or_none()

    async def load(self, session: AsyncSession, *, identity_key: str) -> CompatibilityCampaign | None:
        row = await self.get_row(session, identity_key=identity_key)
        return await self._to_domain(session, row) if row is not None else None

    async def list_for_workspace(
        self, session: AsyncSession, *, workspace_key: str, limit: int = 100
    ) -> list[CompatibilityCampaign]:
        rows = (
            await session.execute(
                select(CompatibilityCampaignModel)
                .where(CompatibilityCampaignModel.workspace_key == workspace_key)
                .order_by(CompatibilityCampaignModel.updated_at.desc(), CompatibilityCampaignModel.identity_key)
                .limit(limit)
            )
        ).scalars().all()
        return [await self._to_domain(session, row) for row in rows]

    async def upsert(
        self, session: AsyncSession, campaign: CompatibilityCampaign, *, recorded_at: datetime | None = None
    ) -> tuple[CompatibilityCampaignModel, bool]:
        """Returns ``(row, created)``."""

        now = recorded_at or datetime.now(UTC)
        await _advisory_lock(session, f"campaign:{campaign.campaign_id}")
        row = await self.get_row(session, identity_key=campaign.campaign_id)
        created = row is None
        if row is None:
            row = CompatibilityCampaignModel(
                id=uuid.uuid4(), identity_key=campaign.campaign_id, workspace_key=campaign.workspace_key,
                change_fingerprint=campaign.change_fingerprint, engine_version=campaign.engine_version,
                dependency_label=campaign.dependency_label[:255], provider_key=campaign.provider_key,
                origin=campaign.origin, producer_repository=campaign.producer_repository,
                old_version=campaign.old_version, new_version=campaign.new_version,
                compatibility=campaign.compatibility, state=campaign.state.value, version=campaign.version,
                blast_json=_blast_json(campaign.org_blast_radius),
                unresolved_risks_json=json.dumps(list(campaign.unresolved_risks)),
                discovered_at=campaign.discovered_at, created_at=now, updated_at=now,
            )
            session.add(row)
            await session.flush()
        else:
            if row.state != campaign.state.value or campaign.version > row.version:
                row.updated_at = now
            row.state = campaign.state.value
            row.version = max(row.version, campaign.version)
            row.blast_json = _blast_json(campaign.org_blast_radius)
            row.unresolved_risks_json = json.dumps(list(campaign.unresolved_risks))

        existing = {
            r.repository: r
            for r in (
                await session.execute(
                    select(CampaignRepositoryRecordModel).where(CampaignRepositoryRecordModel.campaign_id == row.id)
                )
            ).scalars().all()
        }
        keep = {r.repository for r in campaign.records}
        for record in campaign.records:
            record_row = existing.get(record.repository)
            if record_row is None:
                record_row = CampaignRepositoryRecordModel(
                    id=uuid.uuid4(), campaign_id=row.id, repository=record.repository[:512]
                )
                session.add(record_row)
            _fill_record(record_row, record)
        stale = [name for name in existing if name not in keep]
        if stale:
            await session.execute(
                delete(CampaignRepositoryRecordModel).where(
                    CampaignRepositoryRecordModel.campaign_id == row.id,
                    CampaignRepositoryRecordModel.repository.in_(stale),
                )
            )
        await session.flush()
        return row, created

    async def _to_domain(self, session: AsyncSession, row: CompatibilityCampaignModel) -> CompatibilityCampaign:
        record_rows = (
            await session.execute(
                select(CampaignRepositoryRecordModel)
                .where(CampaignRepositoryRecordModel.campaign_id == row.id)
                .order_by(CampaignRepositoryRecordModel.repository)
            )
        ).scalars().all()
        return CompatibilityCampaign(
            campaign_id=row.identity_key, workspace_key=row.workspace_key, change_fingerprint=row.change_fingerprint,
            dependency_label=row.dependency_label, provider_key=row.provider_key, origin=row.origin,
            discovered_at=_aware(row.discovered_at) or row.discovered_at, state=CampaignState(row.state),
            org_blast_radius=_blast_from_json(row.blast_json), records=tuple(_record_from_row(r) for r in record_rows),
            unresolved_risks=tuple(json.loads(row.unresolved_risks_json)), version=row.version,
            engine_version=row.engine_version, producer_repository=row.producer_repository,
            old_version=row.old_version, new_version=row.new_version, compatibility=row.compatibility,
        )


__all__ = ["CampaignStore"]
