"""Context Engine query APIs.

Read-only queries over persisted context bundles/items -- the layer
Phase 5 will call to fetch already-generated context rather than
regenerating it. Mirrors :class:`patchfrog.analysis.queries.AnalysisQueryService`.
"""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from patchfrog.context.domain import ContextItemKind
from patchfrog.persistence.models.context import ContextBundleModel, ContextItemModel
from patchfrog.persistence.repositories import ContextBundleRepository, ContextItemRepository


class ContextQueryService:
    def __init__(self) -> None:
        self._bundle_repo = ContextBundleRepository()
        self._item_repo = ContextItemRepository()

    async def get_context_bundle(
        self, session: AsyncSession, *, bundle_id: uuid.UUID
    ) -> ContextBundleModel | None:
        return await self._bundle_repo.get_by_id(session, bundle_id=bundle_id)

    async def get_context_for_finding(
        self, session: AsyncSession, *, finding_id: uuid.UUID
    ) -> ContextBundleModel | None:
        return await self._bundle_repo.get_succeeded_for_finding(session, finding_id=finding_id)

    async def get_context_for_symbol(
        self, session: AsyncSession, *, symbol_id: uuid.UUID
    ) -> list[ContextBundleModel]:
        return await self._bundle_repo.list_succeeded_for_target_symbol(session, symbol_id=symbol_id)

    async def get_context_items(
        self, session: AsyncSession, *, bundle_id: uuid.UUID
    ) -> list[ContextItemModel]:
        return await self._item_repo.list_for_bundle(session, bundle_id=bundle_id)

    async def get_context_items_by_kind(
        self, session: AsyncSession, *, bundle_id: uuid.UUID, kind: ContextItemKind
    ) -> list[ContextItemModel]:
        return await self._item_repo.list_for_bundle_by_kind(session, bundle_id=bundle_id, kind=kind)
