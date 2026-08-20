"""Review-memory query APIs.

Mirrors :class:`patchfrog.review.queries.ReviewQueryService`'s role: a
thin read-only wrapper over the repositories in
:mod:`patchfrog.persistence.repositories` for presentation/inspection
callers (the CLI's ``review-history`` command today) that don't need --
and shouldn't have to know about -- write-path concerns like the
``pull_request_id``-scoped advisory lock in
:class:`~patchfrog.persistence.repositories.review_generation.ReviewGenerationRepository.create`.
Every method here is a pure read; nothing in this module ever mutates
state or decides "most recent" on its own (that decision belongs solely
to :meth:`~patchfrog.persistence.repositories.review_generation.ReviewGenerationRepository.get_latest_for_pr`,
used only by :mod:`patchfrog.review_memory.service` under proof of
ancestry -- see its own docstring).
"""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from patchfrog.persistence.models.review_memory import (
    ReviewGenerationModel,
    ReviewMemoryTransitionModel,
)
from patchfrog.persistence.repositories.review_generation import ReviewGenerationRepository
from patchfrog.persistence.repositories.review_memory_finding import ReviewMemoryFindingRepository
from patchfrog.persistence.repositories.review_memory_transition import (
    ReviewMemoryTransitionRepository,
)
from patchfrog.review_memory.domain import ReviewMemoryFinding


class ReviewMemoryQueryService:
    def __init__(self) -> None:
        self._generation_repo = ReviewGenerationRepository()
        self._finding_repo = ReviewMemoryFindingRepository()
        self._transition_repo = ReviewMemoryTransitionRepository()

    async def get_review_history_for_pr(
        self, session: AsyncSession, *, pull_request_id: uuid.UUID
    ) -> list[ReviewGenerationModel]:
        """Every review generation for a PR, oldest first -- the full
        incremental sequence, not just the latest."""

        return await self._generation_repo.list_for_pr(session, pull_request_id=pull_request_id)

    async def get_generation(
        self, session: AsyncSession, *, generation_id: uuid.UUID
    ) -> ReviewGenerationModel | None:
        return await self._generation_repo.get_by_id(session, generation_id=generation_id)

    async def get_generation_for_run(
        self, session: AsyncSession, *, review_run_id: uuid.UUID
    ) -> ReviewGenerationModel | None:
        return await self._generation_repo.get_by_review_run_id(session, review_run_id=review_run_id)

    async def get_open_memory_findings(
        self, session: AsyncSession, *, pull_request_id: uuid.UUID
    ) -> list[ReviewMemoryFinding]:
        """Every "live" (open/carried_forward/changed/ambiguous) memory
        finding for a PR -- resolved/superseded findings are excluded.
        A resolved/superseded finding's full history is still inspectable
        via :meth:`get_transitions_for_finding` on its id, discoverable
        through :meth:`get_transitions_for_run`."""

        return await self._finding_repo.get_open_for_pr(session, pull_request_id=pull_request_id)

    async def get_transitions_for_finding(
        self, session: AsyncSession, *, memory_finding_id: uuid.UUID
    ) -> list[ReviewMemoryTransitionModel]:
        """The complete, ordered audit trail for one memory finding --
        every status it has ever held and why."""

        return await self._transition_repo.list_for_finding(session, memory_finding_id=memory_finding_id)

    async def get_transitions_for_run(
        self, session: AsyncSession, *, review_run_id: uuid.UUID
    ) -> list[ReviewMemoryTransitionModel]:
        """Every transition a single review run produced -- what this
        specific run resolved, carried forward, or changed."""

        return await self._transition_repo.list_for_target_run(session, target_review_run_id=review_run_id)

    async def get_already_reported_finding_ids(
        self, session: AsyncSession, *, review_run_id: uuid.UUID
    ) -> frozenset[uuid.UUID]:
        """The exact set :mod:`patchfrog.publishing.planner` suppresses
        for this run -- see
        :meth:`~patchfrog.persistence.repositories.review_memory_finding.ReviewMemoryFindingRepository.list_carried_forward_current_finding_ids`,
        the single source of truth both the publish task and this
        inspection query defer to."""

        return await self._finding_repo.list_carried_forward_current_finding_ids(
            session, review_run_id=review_run_id
        )
