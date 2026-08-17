"""AI Reviewer query APIs.

Read-only queries over persisted review runs/candidates/proposals/critic
verdicts/findings. Mirrors :class:`patchfrog.analysis.queries.AnalysisQueryService`
and :class:`patchfrog.context.queries.ContextQueryService`. ``get_findings_for_run``
is the only query a presentation layer should use to show AI findings to a
user -- everything else here is for audit/debugging.
"""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from patchfrog.persistence.models.review import (
    AIFindingModel,
    AIFindingProposalModel,
    CriticVerdictModel,
    ReviewCandidateModel,
    ReviewRunModel,
)
from patchfrog.persistence.repositories import (
    AIFindingProposalRepository,
    AIFindingRepository,
    CriticVerdictRepository,
    ReviewCandidateRepository,
    ReviewRunRepository,
)


class ReviewQueryService:
    def __init__(self) -> None:
        self._run_repo = ReviewRunRepository()
        self._candidate_repo = ReviewCandidateRepository()
        self._proposal_repo = AIFindingProposalRepository()
        self._verdict_repo = CriticVerdictRepository()
        self._finding_repo = AIFindingRepository()

    async def get_run(self, session: AsyncSession, *, run_id: uuid.UUID) -> ReviewRunModel | None:
        return await self._run_repo.get_by_id(session, run_id=run_id)

    async def get_succeeded_run(
        self,
        session: AsyncSession,
        *,
        repository_id: uuid.UUID,
        commit_sha: str,
        config_fingerprint: str,
        model_fingerprint: str,
    ) -> ReviewRunModel | None:
        return await self._run_repo.get_succeeded(
            session,
            repository_id=repository_id,
            commit_sha=commit_sha,
            config_fingerprint=config_fingerprint,
            model_fingerprint=model_fingerprint,
        )

    async def get_candidates_for_run(
        self, session: AsyncSession, *, review_run_id: uuid.UUID
    ) -> list[ReviewCandidateModel]:
        return await self._candidate_repo.list_for_run(session, review_run_id=review_run_id)

    async def get_all_proposals_for_run(
        self, session: AsyncSession, *, review_run_id: uuid.UUID
    ) -> list[AIFindingProposalModel]:
        """Full audit trail -- every proposal, whatever its final status."""

        return await self._proposal_repo.list_for_run(session, review_run_id=review_run_id)

    async def get_critic_verdict(
        self, session: AsyncSession, *, proposal_id: uuid.UUID
    ) -> CriticVerdictModel | None:
        return await self._verdict_repo.get_for_proposal(session, proposal_id=proposal_id)

    async def get_findings_for_run(
        self, session: AsyncSession, *, review_run_id: uuid.UUID
    ) -> list[AIFindingModel]:
        """The only user-facing query -- findings that survived
        validation, the critic, confidence aggregation, and dedup."""

        return await self._finding_repo.list_for_run(session, review_run_id=review_run_id)
