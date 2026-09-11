"""Data access for :class:`~patchfrog.persistence.models.learning.RepositoryLearningRecordModel`
-- one method per real query/mutation, no derivation logic (that lives
in :mod:`patchfrog.learning_records.service`)."""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterable

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from patchfrog.analysis.domain import FindingCategory
from patchfrog.learning_records.domain import (
    LearningEvidenceRef,
    LearningMaturity,
    LearningSurface,
    LearningType,
    RepositoryLearningRecord,
)
from patchfrog.persistence.models.learning import RepositoryLearningRecordModel


def _to_domain(model: RepositoryLearningRecordModel) -> RepositoryLearningRecord:
    raw_evidence = json.loads(model.evidence_json)
    evidence = tuple(
        LearningEvidenceRef(
            finding_id=uuid.UUID(e["finding_id"]), review_run_id=uuid.UUID(e["review_run_id"]), observed_at=e["observed_at"]
        )
        for e in raw_evidence
    )
    return RepositoryLearningRecord(
        id=model.id,
        repository_id=model.repository_id,
        learning_type=model.learning_type,
        surface=LearningSurface(
            file_path=model.surface_file_path,
            qualified_name=model.surface_qualified_name,
            category=model.surface_category,
        ),
        maturity=model.maturity,
        support_count=model.support_count,
        evidence=evidence,
        first_observed_at=model.first_observed_at,
        last_observed_at=model.last_observed_at,
        retired_reason=model.retired_reason,
        version=model.version,
    )


class RepositoryLearningRecordRepository:
    async def get_by_surface(
        self,
        session: AsyncSession,
        *,
        repository_id: uuid.UUID,
        learning_type: LearningType,
        file_path: str,
        qualified_name: str,
        category: FindingCategory,
    ) -> RepositoryLearningRecordModel | None:
        result = await session.execute(
            select(RepositoryLearningRecordModel).where(
                RepositoryLearningRecordModel.repository_id == repository_id,
                RepositoryLearningRecordModel.learning_type == learning_type,
                RepositoryLearningRecordModel.surface_file_path == file_path,
                RepositoryLearningRecordModel.surface_qualified_name == qualified_name,
                RepositoryLearningRecordModel.surface_category == category,
            )
        )
        return result.scalar_one_or_none()

    async def upsert_many(
        self, session: AsyncSession, *, records: Iterable[RepositoryLearningRecord]
    ) -> tuple[RepositoryLearningRecord, ...]:
        """Idempotent: the same ``(repository_id, learning_type, surface)``
        identity always updates the same row (never a growing duplicate
        history) -- uses a nested-transaction insert-or-update, portable
        across SQLite (tests) and Postgres exactly like
        :mod:`patchfrog_cloud.repositories`' own dedup pattern."""

        persisted: list[RepositoryLearningRecord] = []
        for record in records:
            evidence_json = json.dumps(
                [
                    {"finding_id": str(e.finding_id), "review_run_id": str(e.review_run_id), "observed_at": e.observed_at}
                    for e in record.evidence
                ]
            )
            existing = await self.get_by_surface(
                session,
                repository_id=record.repository_id,
                learning_type=record.learning_type,
                file_path=record.surface.file_path,
                qualified_name=record.surface.qualified_name,
                category=record.surface.category,
            )
            if existing is None:
                model = RepositoryLearningRecordModel(
                    repository_id=record.repository_id,
                    learning_type=record.learning_type,
                    surface_file_path=record.surface.file_path,
                    surface_qualified_name=record.surface.qualified_name,
                    surface_category=record.surface.category,
                    maturity=record.maturity,
                    support_count=record.support_count,
                    evidence_json=evidence_json,
                    first_observed_at=record.first_observed_at,
                    last_observed_at=record.last_observed_at,
                    retired_reason=None,
                    version=record.version,
                )
                try:
                    async with session.begin_nested():
                        session.add(model)
                        await session.flush()
                except IntegrityError:
                    existing = await self.get_by_surface(
                        session,
                        repository_id=record.repository_id,
                        learning_type=record.learning_type,
                        file_path=record.surface.file_path,
                        qualified_name=record.surface.qualified_name,
                        category=record.surface.category,
                    )
                    assert existing is not None
                    model = existing
                else:
                    persisted.append(_to_domain(model))
                    continue
            else:
                model = existing

            model.maturity = record.maturity
            model.support_count = record.support_count
            model.evidence_json = evidence_json
            model.first_observed_at = record.first_observed_at
            model.last_observed_at = record.last_observed_at
            model.retired_reason = None
            model.version = record.version
            await session.flush()
            persisted.append(_to_domain(model))

        return tuple(persisted)

    async def retire_missing(
        self,
        session: AsyncSession,
        *,
        repository_id: uuid.UUID,
        fresh_surface_keys: set[tuple[LearningType, str, str, FindingCategory]],
        retired_reason: str,
    ) -> None:
        """Every existing, non-retired record for ``repository_id`` whose
        ``(learning_type, surface)`` key is *not* in ``fresh_surface_keys``
        (i.e. the most recent recomputation did not reconfirm it) is
        marked ``RETIRED`` -- never deleted, never silently left as if
        still active."""

        result = await session.execute(
            select(RepositoryLearningRecordModel).where(
                RepositoryLearningRecordModel.repository_id == repository_id,
                RepositoryLearningRecordModel.maturity != LearningMaturity.RETIRED,
            )
        )
        for model in result.scalars().all():
            key = (model.learning_type, model.surface_file_path, model.surface_qualified_name, model.surface_category)
            if key not in fresh_surface_keys:
                model.maturity = LearningMaturity.RETIRED
                model.retired_reason = retired_reason
        await session.flush()

    async def list_for_repository(
        self, session: AsyncSession, *, repository_id: uuid.UUID, include_retired: bool = True
    ) -> tuple[RepositoryLearningRecord, ...]:
        stmt = select(RepositoryLearningRecordModel).where(
            RepositoryLearningRecordModel.repository_id == repository_id
        )
        if not include_retired:
            stmt = stmt.where(RepositoryLearningRecordModel.maturity != LearningMaturity.RETIRED)
        result = await session.execute(stmt.order_by(RepositoryLearningRecordModel.support_count.desc()))
        return tuple(_to_domain(m) for m in result.scalars().all())

    async def list_established_for_repositories(
        self, session: AsyncSession, *, repository_ids: tuple[uuid.UUID, ...]
    ) -> tuple[RepositoryLearningRecord, ...]:
        """Used only by :mod:`patchfrog.learning_records.org_aggregation`,
        which supplies ``repository_ids`` explicitly -- this method never
        infers scope itself."""

        if not repository_ids:
            return ()
        result = await session.execute(
            select(RepositoryLearningRecordModel).where(
                RepositoryLearningRecordModel.repository_id.in_(repository_ids),
                RepositoryLearningRecordModel.maturity == LearningMaturity.ESTABLISHED,
            )
        )
        return tuple(_to_domain(m) for m in result.scalars().all())

    async def set_maturity(
        self, session: AsyncSession, *, record_id: uuid.UUID, maturity: LearningMaturity, retired_reason: str | None = None
    ) -> None:
        """Manual override (Y14: "retire a learning manually") -- the next
        recomputation still re-derives from evidence and may reactivate
        it; this is a point-in-time operator action, never a permanent
        evidence override."""

        await session.execute(
            update(RepositoryLearningRecordModel)
            .where(RepositoryLearningRecordModel.id == record_id)
            .values(maturity=maturity, retired_reason=retired_reason)
        )
