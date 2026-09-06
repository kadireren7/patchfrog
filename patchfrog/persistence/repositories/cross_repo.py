"""Persistence operations for :class:`~patchfrog.persistence.models.cross_repo.RepositoryContractKeyModel`/
:class:`~patchfrog.persistence.models.cross_repo.RepositoryRelationModel`.

Every write here is reached only via the trusted-operator-only
``cross-repo`` CLI path (:mod:`patchfrog.cli`) -- never from a review
request, never from ``.patchfrog.yml``, never from any PR-influenced
source (see ``validation/cross_repo_intelligence/latest-summary.md``
section 12)."""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from patchfrog.contract_intelligence.domain import ContractKind
from patchfrog.cross_repo_intelligence.domain import (
    RepositoryRelationKind,
    RepositoryRelationProvenance,
)
from patchfrog.persistence.models.cross_repo import (
    RepositoryContractKeyModel,
    RepositoryRelationModel,
)


class RepositoryContractKeyRepository:
    """Persistence operations for :class:`RepositoryContractKeyModel`."""

    async def get_by_repo_and_key(
        self, session: AsyncSession, *, repository_id: uuid.UUID, stable_key: str
    ) -> RepositoryContractKeyModel | None:
        result = await session.execute(
            select(RepositoryContractKeyModel).where(
                RepositoryContractKeyModel.repository_id == repository_id,
                RepositoryContractKeyModel.stable_key == stable_key,
            )
        )
        return result.scalar_one_or_none()

    async def upsert(
        self,
        session: AsyncSession,
        *,
        repository_id: uuid.UUID,
        contract_kind: ContractKind,
        stable_key: str,
        file_path: str,
        qualified_name: str,
    ) -> RepositoryContractKeyModel:
        existing = await self.get_by_repo_and_key(session, repository_id=repository_id, stable_key=stable_key)
        if existing is not None:
            existing.contract_kind = contract_kind
            existing.file_path = file_path
            existing.qualified_name = qualified_name
            await session.flush()
            return existing

        model = RepositoryContractKeyModel(
            repository_id=repository_id, contract_kind=contract_kind, stable_key=stable_key,
            file_path=file_path, qualified_name=qualified_name,
        )
        session.add(model)
        await session.flush()
        return model

    async def list_for_repository(
        self, session: AsyncSession, *, repository_id: uuid.UUID
    ) -> list[RepositoryContractKeyModel]:
        result = await session.execute(
            select(RepositoryContractKeyModel)
            .where(RepositoryContractKeyModel.repository_id == repository_id)
            .order_by(RepositoryContractKeyModel.stable_key)
        )
        return list(result.scalars().all())

    async def remove(self, session: AsyncSession, *, repository_id: uuid.UUID, stable_key: str) -> bool:
        model = await self.get_by_repo_and_key(session, repository_id=repository_id, stable_key=stable_key)
        if model is None:
            return False
        await session.delete(model)
        await session.flush()
        return True


class RepositoryRelationRepository:
    """Persistence operations for :class:`RepositoryRelationModel`."""

    async def get_by_identity(
        self,
        session: AsyncSession,
        *,
        source_repository_id: uuid.UUID,
        target_repository_id: uuid.UUID,
        relation_kind: RepositoryRelationKind,
        external_contract_key: str,
    ) -> RepositoryRelationModel | None:
        result = await session.execute(
            select(RepositoryRelationModel).where(
                RepositoryRelationModel.source_repository_id == source_repository_id,
                RepositoryRelationModel.target_repository_id == target_repository_id,
                RepositoryRelationModel.relation_kind == relation_kind,
                RepositoryRelationModel.external_contract_key == external_contract_key,
            )
        )
        return result.scalar_one_or_none()

    async def upsert(
        self,
        session: AsyncSession,
        *,
        source_repository_id: uuid.UUID,
        target_repository_id: uuid.UUID,
        relation_kind: RepositoryRelationKind,
        external_contract_key: str,
        provenance: RepositoryRelationProvenance,
    ) -> RepositoryRelationModel:
        existing = await self.get_by_identity(
            session, source_repository_id=source_repository_id, target_repository_id=target_repository_id,
            relation_kind=relation_kind, external_contract_key=external_contract_key,
        )
        if existing is not None:
            existing.provenance = provenance
            existing.active = True
            await session.flush()
            return existing

        model = RepositoryRelationModel(
            source_repository_id=source_repository_id, target_repository_id=target_repository_id,
            relation_kind=relation_kind, external_contract_key=external_contract_key, provenance=provenance,
        )
        session.add(model)
        await session.flush()
        return model

    async def list_for_source(
        self, session: AsyncSession, *, source_repository_id: uuid.UUID
    ) -> list[RepositoryRelationModel]:
        result = await session.execute(
            select(RepositoryRelationModel)
            .where(RepositoryRelationModel.source_repository_id == source_repository_id)
            .order_by(RepositoryRelationModel.created_at)
        )
        return list(result.scalars().all())

    async def deactivate(
        self,
        session: AsyncSession,
        *,
        source_repository_id: uuid.UUID,
        target_repository_id: uuid.UUID,
        relation_kind: RepositoryRelationKind,
        external_contract_key: str,
    ) -> bool:
        model = await self.get_by_identity(
            session, source_repository_id=source_repository_id, target_repository_id=target_repository_id,
            relation_kind=relation_kind, external_contract_key=external_contract_key,
        )
        if model is None or not model.active:
            return False
        model.active = False
        await session.flush()
        return True
