from __future__ import annotations

import uuid
from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from patchfrog.analysis.domain import FindingCategory, Severity
from patchfrog.domain.code import SymbolKind
from patchfrog.persistence.models.review_memory import (
    ReviewMemoryFindingModel,
    ReviewMemoryTransitionModel,
)
from patchfrog.review_memory.domain import (
    EvidenceSnippet,
    FindingMemoryStatus,
    ReviewMemoryFinding,
    TransitionReasonCode,
    parse_evidence_json,
    serialize_evidence,
)


def _to_domain(model: ReviewMemoryFindingModel) -> ReviewMemoryFinding:
    return ReviewMemoryFinding(
        id=model.id,
        source_review_run_id=model.source_review_run_id,
        source_finding_id=model.source_finding_id,
        repository_id=model.repository_id,
        pull_request_id=model.pull_request_id,
        first_seen_commit_sha=model.first_seen_commit_sha,
        last_seen_commit_sha=model.last_seen_commit_sha,
        file_path=model.file_path,
        symbol_id=model.symbol_id,
        symbol_qualified_name=model.symbol_qualified_name,
        symbol_kind=model.symbol_kind,
        category=model.category,
        severity=model.severity,
        title=model.title,
        message=model.message,
        start_line=model.start_line,
        end_line=model.end_line,
        exact_fingerprint=model.exact_fingerprint,
        semantic_family_fingerprint=model.semantic_family_fingerprint,
        status=model.status,
        evidence=parse_evidence_json(model.evidence),
    )


class ReviewMemoryFindingRepository:
    """Persistence for the mutable :class:`ReviewMemoryFindingModel` row
    plus its append-only :class:`ReviewMemoryTransitionModel` audit
    trail -- every mutation here writes exactly one transition row in
    the same call, so the two can never drift apart."""

    async def get_open_for_pr(
        self, session: AsyncSession, *, pull_request_id: uuid.UUID
    ) -> list[ReviewMemoryFinding]:
        """Every "live" (not resolved/superseded) memory finding for a
        PR, one query -- the input to
        :meth:`patchfrog.review_memory.resolver.ReviewMemoryResolver.resolve_pre_review`.
        Never N+1: callers must never loop calling a per-finding lookup."""

        result = await session.execute(
            select(ReviewMemoryFindingModel).where(
                ReviewMemoryFindingModel.pull_request_id == pull_request_id,
                ReviewMemoryFindingModel.status.in_(
                    [
                        FindingMemoryStatus.OPEN,
                        FindingMemoryStatus.CARRIED_FORWARD,
                        FindingMemoryStatus.CHANGED,
                        FindingMemoryStatus.AMBIGUOUS,
                    ]
                ),
            )
        )
        return [_to_domain(m) for m in result.scalars().all()]

    async def list_carried_forward_current_finding_ids(
        self, session: AsyncSession, *, review_run_id: uuid.UUID
    ) -> frozenset[uuid.UUID]:
        """The set of this run's :class:`~patchfrog.persistence.models.review.AIFindingModel`
        ids that are actually a rechecked, unchanged continuation of an
        earlier memory finding -- i.e. already reported to the PR in a
        previous publication. This is the *only* input
        :mod:`patchfrog.publishing.planner`'s ``already_reported_finding_ids``
        needs; :meth:`patchfrog.publishing.service.ReviewPublicationService.publish`
        is keyed only by ``review_run_id``, so this is deliberately
        derivable from that alone -- no dependency on the review task
        having passed anything through in-process."""

        result = await session.execute(
            select(ReviewMemoryFindingModel.current_finding_id).where(
                ReviewMemoryFindingModel.current_review_run_id == review_run_id,
                ReviewMemoryFindingModel.status == FindingMemoryStatus.CARRIED_FORWARD,
                ReviewMemoryFindingModel.current_finding_id.is_not(None),
            )
        )
        return frozenset(fid for fid in result.scalars().all() if fid is not None)

    async def get_by_semantic_family(
        self, session: AsyncSession, *, pull_request_id: uuid.UUID, semantic_family_fingerprint: str
    ) -> ReviewMemoryFinding | None:
        result = await session.execute(
            select(ReviewMemoryFindingModel).where(
                ReviewMemoryFindingModel.pull_request_id == pull_request_id,
                ReviewMemoryFindingModel.semantic_family_fingerprint == semantic_family_fingerprint,
                ReviewMemoryFindingModel.status.in_(
                    [
                        FindingMemoryStatus.OPEN,
                        FindingMemoryStatus.CARRIED_FORWARD,
                        FindingMemoryStatus.CHANGED,
                        FindingMemoryStatus.AMBIGUOUS,
                    ]
                ),
            )
        )
        return _to_domain(m) if (m := result.scalar_one_or_none()) is not None else None

    async def create(
        self,
        session: AsyncSession,
        *,
        repository_id: uuid.UUID,
        pull_request_id: uuid.UUID,
        source_review_run_id: uuid.UUID,
        source_finding_id: uuid.UUID,
        commit_sha: str,
        file_path: str,
        symbol_id: uuid.UUID | None,
        symbol_qualified_name: str | None,
        symbol_kind: SymbolKind | None,
        category: FindingCategory,
        severity: Severity,
        title: str,
        message: str,
        start_line: int,
        end_line: int,
        exact_fingerprint: str,
        semantic_family_fingerprint: str,
        evidence: Sequence[EvidenceSnippet] = (),
    ) -> ReviewMemoryFinding:
        model = ReviewMemoryFindingModel(
            repository_id=repository_id,
            pull_request_id=pull_request_id,
            source_review_run_id=source_review_run_id,
            source_finding_id=source_finding_id,
            current_finding_id=source_finding_id,
            current_review_run_id=source_review_run_id,
            first_seen_commit_sha=commit_sha,
            last_seen_commit_sha=commit_sha,
            file_path=file_path,
            symbol_id=symbol_id,
            symbol_qualified_name=symbol_qualified_name,
            symbol_kind=symbol_kind,
            category=category,
            severity=severity,
            title=title,
            message=message,
            start_line=start_line,
            end_line=end_line,
            exact_fingerprint=exact_fingerprint,
            semantic_family_fingerprint=semantic_family_fingerprint,
            evidence=serialize_evidence(evidence),
            status=FindingMemoryStatus.OPEN,
        )
        session.add(model)
        await session.flush()
        transition = ReviewMemoryTransitionModel(
            memory_finding_id=model.id,
            source_review_run_id=None,
            target_review_run_id=source_review_run_id,
            old_status=None,
            new_status=FindingMemoryStatus.OPEN,
            reason=TransitionReasonCode.NEW_FINDING,
            detail="first accepted finding for this semantic family",
        )
        session.add(transition)
        await session.flush()
        return _to_domain(model)

    async def apply_transition(
        self,
        session: AsyncSession,
        *,
        memory_finding_id: uuid.UUID,
        new_status: FindingMemoryStatus,
        reason: TransitionReasonCode,
        detail: str,
        target_review_run_id: uuid.UUID,
        commit_sha: str,
        updated_file_path: str | None = None,
        updated_start_line: int | None = None,
        updated_end_line: int | None = None,
        updated_symbol_id: uuid.UUID | None = None,
        updated_current_finding_id: uuid.UUID | None = None,
        updated_evidence: Sequence[EvidenceSnippet] | None = None,
    ) -> ReviewMemoryFinding:
        model = await session.get(ReviewMemoryFindingModel, memory_finding_id)
        if model is None:
            raise ValueError(f"No review memory finding with id {memory_finding_id}")

        old_status = model.status
        previous_review_run_id = model.current_review_run_id
        model.status = new_status
        model.last_seen_commit_sha = commit_sha
        model.current_review_run_id = target_review_run_id
        if updated_file_path is not None:
            model.file_path = updated_file_path
        if updated_start_line is not None:
            model.start_line = updated_start_line
        if updated_end_line is not None:
            model.end_line = updated_end_line
        if updated_symbol_id is not None:
            model.symbol_id = updated_symbol_id
        if updated_current_finding_id is not None:
            model.current_finding_id = updated_current_finding_id
        elif new_status in (FindingMemoryStatus.RESOLVED, FindingMemoryStatus.SUPERSEDED):
            model.current_finding_id = None
        if updated_evidence is not None:
            # Only ever set on a genuine fresh AI reconfirmation
            # (RECHECK_CONFIRMED) -- a pure zero-AI-call carry-forward
            # never passes this, since UNCHANGED/MOVED/RENAMED symbol
            # continuity already guarantees the existing evidence is
            # still exactly as valid to check against next time.
            model.evidence = serialize_evidence(updated_evidence)

        session.add(
            ReviewMemoryTransitionModel(
                memory_finding_id=model.id,
                source_review_run_id=previous_review_run_id,
                target_review_run_id=target_review_run_id,
                old_status=old_status,
                new_status=new_status,
                reason=reason,
                detail=detail,
            )
        )
        await session.flush()
        return _to_domain(model)
