"""Idempotent persistence of upstream changes and their impact (M6.9).

- :meth:`UpstreamChangeStore.record_event` -- one row per event
  fingerprint; re-ingesting the same change only refreshes
  ``last_observed_at`` (diff items are written once: a fingerprint fully
  determines them).
- :meth:`record_impact` -- one row per (event, repository, matched
  dependency key, analyzed commit); re-analysis of the same state updates
  it in place, a new commit adds a new row (history).
- registry reads -- the old side of a registry-backed diff (the latest M5
  contract snapshot) and cross-repository consumer rows -- reuse M5's
  tables directly; nothing is copied.

Writes for one fingerprint are serialized with a transaction-scoped
PostgreSQL advisory lock (a no-op on SQLite), the same pattern as the M5
registry.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import asdict
from datetime import UTC, datetime

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from patchfrog.dependencies.domain import DependencyUsageSite, DetectionConfidence, EvidenceType
from patchfrog.dependencies.registry import STATUS_ACTIVE
from patchfrog.persistence.models.dependency import (
    ExternalContractSnapshotModel,
    ExternalDependencyModel,
    ExternalDependencyUsageSiteModel,
)
from patchfrog.persistence.models.repository import RepositoryModel
from patchfrog.persistence.models.upstream import (
    ExternalChangeDiffItemModel,
    ExternalChangeEventModel,
    ExternalChangeImpactModel,
)
from patchfrog.upstream.blast_radius import blast_radius_to_dict
from patchfrog.upstream.consumers import ConsumerImpact, DependencyIdentity
from patchfrog.upstream.domain import ExternalChangeEvent
from patchfrog.upstream.report import consumer_impact_to_dict
from patchfrog.upstream.workspace import RegistryDependency, RepositoryImpact

MAX_JSON_COLUMN_BYTES = 256_000


def _bounded(value: object) -> str:
    rendered = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    if len(rendered.encode()) <= MAX_JSON_COLUMN_BYTES:
        return rendered
    return json.dumps({"truncated": True, "bytes": len(rendered.encode())})


def _aware(value: datetime) -> datetime:
    """SQLite returns naive datetimes; every stored value is UTC."""

    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


async def _advisory_lock(session: AsyncSession, key: str) -> None:
    if session.bind is None or session.bind.dialect.name != "postgresql":
        return
    digest = hashlib.sha256(key.encode()).digest()[:8]
    await session.execute(
        text("SELECT pg_advisory_xact_lock(:key)"), {"key": int.from_bytes(digest, "big") & 0x7FFFFFFFFFFFFFFF}
    )


class UpstreamChangeStore:
    async def record_event(
        self, session: AsyncSession, event: ExternalChangeEvent
    ) -> tuple[ExternalChangeEventModel, bool]:
        """Returns (row, created)."""

        await _advisory_lock(session, f"upstream-change:{event.fingerprint}")
        existing = await self.get_event(session, event.fingerprint)
        if existing is not None:
            existing.last_observed_at = max(_aware(existing.last_observed_at), _aware(event.observed_at))
            await session.flush()
            return existing, False
        target = event.target
        row = ExternalChangeEventModel(
            fingerprint=event.fingerprint,
            engine_version=event.engine_version,
            provider_key=target.provider_key,
            ecosystem=target.ecosystem.value if target.ecosystem else None,
            package_name=target.package_name,
            dependency_key=target.dependency_key,
            target_label=target.label[:255],
            source_type=event.source.value,
            change_kind=event.kind.value,
            source_ref=event.source_ref[:1024],
            old_version=event.old.version[:128] if event.old.version else None,
            new_version=event.new.version[:128] if event.new.version else None,
            old_contract_fingerprint=event.old.fingerprint,
            new_contract_fingerprint=event.new.fingerprint,
            contract_format=event.new.contract_format,
            risk=event.classification.risk.value,
            compatibility=event.classification.compatibility.value,
            reasons_json=json.dumps(list(event.classification.reasons)),
            counts_json=json.dumps(dict(event.classification.counts), sort_keys=True),
            diff_item_count=len(event.diff),
            truncated=event.truncated,
            hints_fingerprint=event.hints_fingerprint,
            hints_provenance=event.hints_provenance,
            release_json=json.dumps(asdict(event.release), sort_keys=True) if event.release else None,
            notes_json=json.dumps(list(event.notes)),
            first_observed_at=event.observed_at,
            last_observed_at=event.observed_at,
        )
        session.add(row)
        await session.flush()
        for item in event.diff:
            session.add(
                ExternalChangeDiffItemModel(
                    event_id=row.id,
                    item_key=item.key,
                    kind=item.kind.value,
                    location=item.location[:1024],
                    subject_json=json.dumps(item.subject.as_dict(), sort_keys=True),
                    compatibility=item.compatibility.value,
                    old_repr=item.old,
                    new_repr=item.new,
                    explanation=item.explanation,
                    evidence_json=json.dumps(list(item.evidence)),
                    replacement=item.replacement[:512] if item.replacement else None,
                )
            )
        await session.flush()
        return row, True

    async def get_event(self, session: AsyncSession, fingerprint: str) -> ExternalChangeEventModel | None:
        return (
            await session.execute(
                select(ExternalChangeEventModel).where(ExternalChangeEventModel.fingerprint == fingerprint)
            )
        ).scalar_one_or_none()

    async def diff_items(self, session: AsyncSession, event_id: uuid.UUID) -> list[ExternalChangeDiffItemModel]:
        query = (
            select(ExternalChangeDiffItemModel)
            .where(ExternalChangeDiffItemModel.event_id == event_id)
            .order_by(ExternalChangeDiffItemModel.location, ExternalChangeDiffItemModel.kind)
        )
        return list((await session.execute(query)).scalars().all())

    async def list_events(
        self, session: AsyncSession, *, package_name: str | None = None, provider_key: str | None = None
    ) -> list[ExternalChangeEventModel]:
        query = select(ExternalChangeEventModel)
        if package_name is not None:
            query = query.where(ExternalChangeEventModel.package_name == package_name)
        if provider_key is not None:
            query = query.where(ExternalChangeEventModel.provider_key == provider_key)
        query = query.order_by(ExternalChangeEventModel.first_observed_at, ExternalChangeEventModel.fingerprint)
        return list((await session.execute(query)).scalars().all())

    async def record_impact(
        self,
        session: AsyncSession,
        *,
        event_id: uuid.UUID,
        repository_id: uuid.UUID,
        impact: RepositoryImpact,
        dependency_ids: dict[str, uuid.UUID] | None = None,
        analyzed_at: datetime | None = None,
    ) -> list[tuple[ExternalChangeImpactModel, bool]]:
        """One row per matched dependency (or a single row with an empty
        dependency key when the repository does not use the dependency)."""

        now = analyzed_at or datetime.now(UTC)
        commit = impact.commit_sha or ""
        await _advisory_lock(session, f"upstream-impact:{event_id}:{repository_id}:{commit}")
        radii = {r.dependency_key: r for r in impact.blast_radii}
        entries: list[tuple[str, ConsumerImpact | None]] = [(i.dependency_key, i) for i in impact.consumer_impacts]
        matched_without_consumers = [m.identity.key for m in impact.matches
                                     if m.identity.key not in {k for k, _ in entries}]
        entries += [(key, None) for key in matched_without_consumers]
        if not entries:
            entries = [("", None)]
        results: list[tuple[ExternalChangeImpactModel, bool]] = []
        for dependency_key, consumer_impact in entries:
            radius = radii.get(dependency_key)
            summary = radius.summary() if radius else {}
            fields = {
                "dependency_id": (dependency_ids or {}).get(dependency_key),
                "status": impact.status.value,
                "reason": impact.reason[:255],
                "direct_count": len(consumer_impact.direct) if consumer_impact else 0,
                "transitive_count": summary.get("transitive", 0),
                "potential_count": len(consumer_impact.potential) if consumer_impact else 0,
                "related_test_count": summary.get("related_tests", 0),
                "consumers_json": _bounded(consumer_impact_to_dict(consumer_impact) if consumer_impact else {}),
                "blast_radius_json": _bounded(blast_radius_to_dict(radius) if radius else {}),
            }
            row = (
                await session.execute(
                    select(ExternalChangeImpactModel).where(
                        ExternalChangeImpactModel.event_id == event_id,
                        ExternalChangeImpactModel.repository_id == repository_id,
                        ExternalChangeImpactModel.dependency_key == dependency_key,
                        ExternalChangeImpactModel.analyzed_commit_sha == commit,
                    )
                )
            ).scalar_one_or_none()
            if row is None:
                row = ExternalChangeImpactModel(
                    event_id=event_id, repository_id=repository_id, dependency_key=dependency_key,
                    analyzed_commit_sha=commit, first_analyzed_at=now, last_analyzed_at=now, **fields,
                )
                session.add(row)
                results.append((row, True))
            else:
                for name, value in fields.items():
                    setattr(row, name, value)
                row.last_analyzed_at = now
                results.append((row, False))
        await session.flush()
        return results

    async def impacts_for_event(self, session: AsyncSession, event_id: uuid.UUID) -> list[ExternalChangeImpactModel]:
        query = (
            select(ExternalChangeImpactModel)
            .where(ExternalChangeImpactModel.event_id == event_id)
            .order_by(ExternalChangeImpactModel.repository_id, ExternalChangeImpactModel.dependency_key)
        )
        return list((await session.execute(query)).scalars().all())

    # -- M5 registry reads (reuse, never copy) ----------------------------------

    async def latest_contract_snapshot(
        self, session: AsyncSession, *, repository_id: uuid.UUID, dependency_key: str
    ) -> tuple[ExternalDependencyModel, ExternalContractSnapshotModel] | None:
        dependency = (
            await session.execute(
                select(ExternalDependencyModel).where(
                    ExternalDependencyModel.repository_id == repository_id,
                    ExternalDependencyModel.dependency_key == dependency_key,
                )
            )
        ).scalar_one_or_none()
        if dependency is None:
            return None
        snapshot = (
            await session.execute(
                select(ExternalContractSnapshotModel)
                .where(ExternalContractSnapshotModel.dependency_id == dependency.id)
                .order_by(ExternalContractSnapshotModel.last_observed_at.desc(),
                          ExternalContractSnapshotModel.fingerprint)
                .limit(1)
            )
        ).scalar_one_or_none()
        return (dependency, snapshot) if snapshot is not None else None

    async def registry_dependencies(
        self, session: AsyncSession
    ) -> tuple[list[RegistryDependency], list[str], dict[tuple[str, str], uuid.UUID], dict[str, uuid.UUID]]:
        """Every active registry dependency (with usage sites) across all
        repositories, every repository name, the dependency row ids and
        the repository ids -- the cross-repository view of M6.7."""

        repositories = list((await session.execute(select(RepositoryModel))).scalars().all())
        repo_names = {r.id: r.full_name for r in repositories}
        rows = list(
            (
                await session.execute(
                    select(ExternalDependencyModel).where(ExternalDependencyModel.status == STATUS_ACTIVE)
                )
            ).scalars().all()
        )
        site_rows = list(
            (
                await session.execute(
                    select(ExternalDependencyUsageSiteModel).where(
                        ExternalDependencyUsageSiteModel.dependency_id.in_([r.id for r in rows])
                    )
                )
            ).scalars().all()
        ) if rows else []
        sites_by_dependency: dict[uuid.UUID, list[DependencyUsageSite]] = {}
        for site in site_rows:
            sites_by_dependency.setdefault(site.dependency_id, []).append(
                DependencyUsageSite(
                    file_path=site.file_path, line=site.line or None, symbol=site.symbol,
                    evidence_type=EvidenceType(site.evidence_type), token=site.token,
                    confidence=DetectionConfidence(site.confidence),
                )
            )
        result: list[RegistryDependency] = []
        dependency_ids: dict[tuple[str, str], uuid.UUID] = {}
        for row in rows:
            name = repo_names.get(row.repository_id, str(row.repository_id))
            metadata = json.loads(row.metadata_json or "{}")
            hosts = {
                h for value in (metadata.get("api_hosts"), metadata.get("server_hosts")) if value
                for h in str(value).split(",") if h
            }
            identity = DependencyIdentity(
                key=row.dependency_key, provider_key=row.provider_key, kind=row.kind, ecosystem=row.ecosystem,
                package_name=row.package_name, contract_fingerprint=row.contract_fingerprint,
                hosts=tuple(sorted(hosts)), declared_version=row.declared_version,
                resolved_version=row.resolved_version,
            )
            sites = tuple(sorted(sites_by_dependency.get(row.id, []),
                                 key=lambda s: (s.file_path, s.line or 0, s.evidence_type.value, s.token)))
            result.append(RegistryDependency(name, row.last_observed_commit_sha, identity, sites))
            dependency_ids[(name, row.dependency_key)] = row.id
        return result, sorted(repo_names.values()), dependency_ids, {v: k for k, v in repo_names.items()}


__all__ = ["MAX_JSON_COLUMN_BYTES", "UpstreamChangeStore"]
