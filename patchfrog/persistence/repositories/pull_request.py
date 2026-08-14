from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from patchfrog.persistence.models.pull_request import PullRequestModel


class PullRequestRepository:
    """Persistence operations for :class:`PullRequestModel`."""

    async def get_by_repository_and_number(
        self, session: AsyncSession, *, repository_id: uuid.UUID, github_pr_number: int
    ) -> PullRequestModel | None:
        result = await session.execute(
            select(PullRequestModel).where(
                PullRequestModel.repository_id == repository_id,
                PullRequestModel.github_pr_number == github_pr_number,
            )
        )
        return result.scalar_one_or_none()

    async def upsert(
        self,
        session: AsyncSession,
        *,
        repository_id: uuid.UUID,
        github_pr_number: int,
        title: str,
        author: str,
        base_sha: str,
        head_sha: str,
        state: str,
    ) -> PullRequestModel:
        existing = await self.get_by_repository_and_number(
            session, repository_id=repository_id, github_pr_number=github_pr_number
        )
        if existing is not None:
            existing.title = title
            existing.author = author
            existing.base_sha = base_sha
            existing.head_sha = head_sha
            existing.state = state
            await session.flush()
            return existing

        model = PullRequestModel(
            repository_id=repository_id,
            github_pr_number=github_pr_number,
            title=title,
            author=author,
            base_sha=base_sha,
            head_sha=head_sha,
            state=state,
        )
        session.add(model)
        await session.flush()
        return model
