from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from patchfrog.persistence.models.repository import RepositoryModel


class RepositoryRepository:
    """Persistence operations for :class:`RepositoryModel`."""

    async def get_by_github_id(
        self, session: AsyncSession, *, github_repository_id: int
    ) -> RepositoryModel | None:
        result = await session.execute(
            select(RepositoryModel).where(
                RepositoryModel.github_repository_id == github_repository_id
            )
        )
        return result.scalar_one_or_none()

    async def upsert(
        self,
        session: AsyncSession,
        *,
        github_repository_id: int,
        owner: str,
        name: str,
        full_name: str,
        installation_id: int,
    ) -> RepositoryModel:
        """Create the repository row if missing, otherwise refresh mutable fields."""

        existing = await self.get_by_github_id(session, github_repository_id=github_repository_id)
        if existing is not None:
            existing.owner = owner
            existing.name = name
            existing.full_name = full_name
            existing.installation_id = installation_id
            await session.flush()
            return existing

        model = RepositoryModel(
            github_repository_id=github_repository_id,
            owner=owner,
            name=name,
            full_name=full_name,
            installation_id=installation_id,
        )
        session.add(model)
        await session.flush()
        return model
