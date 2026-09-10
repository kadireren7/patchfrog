"""Builds a :class:`~patchfrog.agent_handoff.domain.FindingHandoff` from
already-persisted PatchFrog state -- Milestone T (T1).

Deliberately a pure, deterministic projection: no LLM call, no re-scoring,
no re-judging (see the module docstring of
:mod:`patchfrog.agent_handoff.domain` for exactly what is and is not
included, and why). Structurally never imports
:mod:`patchfrog.config.settings`, :mod:`patchfrog.github`, or any provider
module -- a handoff-generation failure is always "not enough persisted
evidence," never a credential or network failure (enforced by
``tests/unit/test_agent_handoff_module_boundaries.py``).
"""

from __future__ import annotations

import json
import uuid
from enum import StrEnum

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from patchfrog.agent_handoff.domain import (
    FINDING_HANDOFF_SCHEMA_VERSION,
    MAX_HANDOFF_EVIDENCE_ITEMS,
    FindingHandoff,
    HandoffEvidenceSnippet,
    bound_text,
    compute_handoff_id,
)
from patchfrog.executable_verification.eligibility import determine_verification_target
from patchfrog.persistence.models.publishing import (
    ReviewPublicationCommentModel,
    ReviewPublicationModel,
)
from patchfrog.persistence.models.repository import RepositoryModel
from patchfrog.persistence.models.review import ReviewCandidateModel
from patchfrog.persistence.repositories import (
    AIFindingRepository,
    CriticVerdictRepository,
    RepositoryRepository,
    ReviewCandidateRepository,
    ReviewRunRepository,
)
from patchfrog.publishing.domain import ReviewPublicationStatus
from patchfrog.review.domain import ReviewCandidate, ReviewCandidateReason
from patchfrog.review.redaction import redact_secrets


class HandoffUnavailableReason(StrEnum):
    """Why a handoff could not be built -- always returned honestly
    (Part H: "do not invent missing evidence") rather than a generic
    failure."""

    FINDING_NOT_FOUND = "finding_not_found"
    #: Integrity error -- a finding row exists but its parent candidate/run
    #: row does not. Should never happen (both are CASCADE-linked FKs) but
    #: handled explicitly rather than raising, since this module must
    #: never crash the MCP process on an unexpected data shape.
    CANDIDATE_NOT_FOUND = "candidate_not_found"
    REVIEW_RUN_NOT_FOUND = "review_run_not_found"
    REPOSITORY_NOT_FOUND = "repository_not_found"


class AgentHandoffService:
    def __init__(self) -> None:
        self._finding_repo = AIFindingRepository()
        self._candidate_repo = ReviewCandidateRepository()
        self._run_repo = ReviewRunRepository()
        self._repository_repo = RepositoryRepository()
        self._verdict_repo = CriticVerdictRepository()

    async def build_handoff(
        self, session: AsyncSession, *, finding_id: uuid.UUID
    ) -> tuple[FindingHandoff | None, HandoffUnavailableReason | None]:
        finding = await self._finding_repo.get_by_id(session, finding_id=finding_id)
        if finding is None:
            return None, HandoffUnavailableReason.FINDING_NOT_FOUND

        candidate_model = await session.get(ReviewCandidateModel, finding.candidate_id)
        if candidate_model is None:
            return None, HandoffUnavailableReason.CANDIDATE_NOT_FOUND

        run = await self._run_repo.get_by_id(session, run_id=finding.review_run_id)
        if run is None:
            return None, HandoffUnavailableReason.REVIEW_RUN_NOT_FOUND

        repository = await session.get(RepositoryModel, run.repository_id)
        if repository is None:
            return None, HandoffUnavailableReason.REPOSITORY_NOT_FOUND

        critic_verdict = await self._verdict_repo.get_for_proposal(session, proposal_id=finding.proposal_id)

        published, github_review_id = await self._publication_status(session, finding_id=finding.id)

        evidence = tuple(
            HandoffEvidenceSnippet(
                file_path=str(e.get("file_path", "")),
                start_line=int(e.get("start_line", 0)),
                end_line=int(e.get("end_line", 0)),
                quoted_text=bound_text(redact_secrets(str(e.get("quoted_text", ""))).text),
            )
            for e in json.loads(finding.evidence or "[]")[:MAX_HANDOFF_EVIDENCE_ITEMS]
        )

        static_finding_ids = tuple(uuid.UUID(i) for i in json.loads(finding.static_finding_ids or "[]"))

        suggested_verification_target = self._suggested_verification_target(candidate_model)

        handoff = FindingHandoff(
            handoff_id=compute_handoff_id(
                repository_id=repository.id,
                finding_id=finding.id,
                review_run_id=run.id,
                original_commit_sha=run.commit_sha,
            ),
            schema_version=FINDING_HANDOFF_SCHEMA_VERSION,
            repository_id=repository.id,
            repository_full_name=repository.full_name,
            review_run_id=run.id,
            original_commit_sha=run.commit_sha,
            finding_id=finding.id,
            category=finding.category,
            severity=finding.severity,
            confidence=finding.confidence,
            title=bound_text(redact_secrets(finding.title).text),
            message=bound_text(redact_secrets(finding.message).text),
            reasoning_summary=bound_text(redact_secrets(finding.reasoning_summary).text),
            impact=bound_text(redact_secrets(finding.impact).text) if finding.impact else None,
            suggested_fix=bound_text(redact_secrets(finding.suggested_fix).text) if finding.suggested_fix else None,
            file_path=finding.file_path,
            start_line=finding.start_line,
            end_line=finding.end_line,
            qualified_name=candidate_model.qualified_name,
            evidence=evidence,
            corroborated_by_static=finding.corroborated_by_static,
            static_finding_ids=static_finding_ids,
            critic_reasoning_summary=(
                bound_text(redact_secrets(critic_verdict.reasoning_summary).text)
                if critic_verdict is not None
                else None
            ),
            suggested_verification_target=suggested_verification_target,
            executable_verification_evidence=None,
            published=published,
            github_review_id=github_review_id,
        )
        return handoff, None

    @staticmethod
    def _suggested_verification_target(candidate_model: ReviewCandidateModel) -> str | None:
        """Deterministically re-derived, never assumed from the original
        review (Part Z) -- reconstructs a :class:`ReviewCandidate` from
        persisted fields and calls the exact same eligibility primitive
        Milestone S/S6 use, with an empty companions set (a full Change
        Intelligence recompute against an arbitrary later point is out of
        scope for a bounded handoff -- see
        ``validation/agent_handoff/latest-summary.md`` section 2.3)."""

        candidate = ReviewCandidate(
            file_path=candidate_model.file_path,
            symbol_id=candidate_model.symbol_id,
            symbol_name=candidate_model.symbol_name,
            qualified_name=candidate_model.qualified_name,
            start_line=candidate_model.start_line,
            end_line=candidate_model.end_line,
            changed_lines=tuple(json.loads(candidate_model.changed_lines or "[]")),
            static_finding_ids=(),
            reason=ReviewCandidateReason(candidate_model.reason),
        )
        return determine_verification_target(candidate=candidate, expected_companions=())

    async def _publication_status(
        self, session: AsyncSession, *, finding_id: uuid.UUID
    ) -> tuple[bool, int | None]:
        result = await session.execute(
            select(ReviewPublicationModel.github_review_id)
            .join(
                ReviewPublicationCommentModel,
                ReviewPublicationCommentModel.review_publication_id == ReviewPublicationModel.id,
            )
            .where(
                ReviewPublicationCommentModel.finding_id == finding_id,
                ReviewPublicationModel.status == ReviewPublicationStatus.PUBLISHED,
            )
            .limit(1)
        )
        row = result.first()
        if row is None:
            return False, None
        return True, row[0]
