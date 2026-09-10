"""Real, DB-backed corpus for Merge Readiness (Milestone V). Every test
builds real `RepositoryModel`/`PullRequestModel`/`ReviewRunModel`/
`AIFindingModel` rows (never hand-built domain objects standing in for a
round trip) and asserts on `MergeReadinessService.evaluate`'s real
output. `FeedbackAssessmentModel`/`FixAttemptModel` rows are built via
their own real repositories, exactly as production code would create
them."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from patchfrog.analysis.domain import Confidence, FindingCategory, Severity
from patchfrog.feedback.domain import (
    FEEDBACK_ASSESSMENT_VERSION,
    FeedbackAssessment,
    ResolutionState,
    SignalPolarity,
)
from patchfrog.fix_verification.domain import FixAttemptStatus
from patchfrog.merge_readiness.domain import MergeReadinessDecision, MergeReadinessReasonCode
from patchfrog.merge_readiness.service import MergeReadinessService
from patchfrog.persistence.models.review import (
    AIFindingModel,
    AIFindingProposalModel,
    ReviewCandidateModel,
    ReviewRunModel,
)
from patchfrog.persistence.repositories import RepositoryRepository
from patchfrog.persistence.repositories.feedback import FeedbackAssessmentRepository
from patchfrog.persistence.repositories.fix_attempt import FixAttemptRepository
from patchfrog.persistence.repositories.pull_request import PullRequestRepository
from patchfrog.review.agents.roles import AgentRole
from patchfrog.review.domain import ProposalStatus, ReviewCandidateReason, ReviewRunStatus

_ORIGINAL_SHA = "a" * 40
_HEAD_SHA = "b" * 40


async def _make_repository(session_factory: async_sessionmaker[AsyncSession], full_name: str) -> uuid.UUID:
    async with session_factory() as session:
        repo = await RepositoryRepository().upsert(
            session, github_repository_id=abs(hash(full_name)) % (2**62),
            owner="test", name=full_name.split("/")[-1], full_name=full_name, installation_id=1,
        )
        await session.commit()
        return repo.id


async def _make_pull_request(
    session_factory: async_sessionmaker[AsyncSession], *, repository_id: uuid.UUID, number: int, head_sha: str,
) -> uuid.UUID:
    async with session_factory() as session:
        pr = await PullRequestRepository().upsert(
            session, repository_id=repository_id, github_pr_number=number, title="t", author="a",
            base_sha=_ORIGINAL_SHA, head_sha=head_sha, state="open",
        )
        await session.commit()
        return pr.id


async def _stage_run_with_findings(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    repository_id: uuid.UUID,
    pull_request_id: uuid.UUID,
    commit_sha: str,
    status: ReviewRunStatus = ReviewRunStatus.SUCCEEDED,
    findings: list[tuple[Severity, FindingCategory]] | None = None,
) -> tuple[uuid.UUID, list[uuid.UUID]]:
    async with session_factory() as session:
        run = ReviewRunModel(
            id=uuid.uuid4(), repository_id=repository_id, repository_index_id=uuid.uuid4(),
            pull_request_id=pull_request_id, commit_sha=commit_sha, config_fingerprint=uuid.uuid4().hex,
            model_fingerprint=uuid.uuid4().hex, incremental_context_fingerprint="i" * 64, status=status,
            reviewer_provider="fake", reviewer_model="fake-model",
            started_at=datetime.now(UTC), completed_at=datetime.now(UTC) if status is ReviewRunStatus.SUCCEEDED else None,
        )
        session.add(run)
        await session.flush()

        finding_ids: list[uuid.UUID] = []
        for severity, category in findings or []:
            candidate = ReviewCandidateModel(
                id=uuid.uuid4(), review_run_id=run.id, file_path="m.py", symbol_id=None,
                symbol_name="f", qualified_name="f", start_line=1, end_line=2,
                changed_lines=json.dumps([1]), reason=ReviewCandidateReason.CHANGED_SYMBOL,
            )
            session.add(candidate)
            await session.flush()

            proposal = AIFindingProposalModel(
                id=uuid.uuid4(), review_run_id=run.id, candidate_id=candidate.id, title="t",
                message="m", category=category, severity=severity, confidence=Confidence.HIGH,
                file_path="m.py", start_line=1, end_line=2, evidence="[]", reasoning_summary="r",
                status=ProposalStatus.ACCEPTED, agent_role=AgentRole.CORRECTNESS,
            )
            session.add(proposal)
            await session.flush()

            finding = AIFindingModel(
                id=uuid.uuid4(), review_run_id=run.id, proposal_id=proposal.id, candidate_id=candidate.id,
                title="t", message="m", category=category, severity=severity, confidence=Confidence.HIGH,
                file_path="m.py", start_line=1, end_line=2, evidence="[]", reasoning_summary="r",
                agent_role=AgentRole.CORRECTNESS,
            )
            session.add(finding)
            await session.flush()
            finding_ids.append(finding.id)

        await session.commit()
        return run.id, finding_ids


async def _dismiss_finding(session_factory: async_sessionmaker[AsyncSession], *, finding_id: uuid.UUID) -> None:
    async with session_factory() as session:
        await FeedbackAssessmentRepository().upsert(
            session,
            assessment=FeedbackAssessment(
                finding_id=finding_id, usefulness_signal=SignalPolarity.NEGATIVE,
                correctness_signal=SignalPolarity.NEGATIVE, resolution_signal=ResolutionState.CLOSED,
                engagement_signal=True, confidence=None, reasons=("test dismissal",),
                assessment_version=FEEDBACK_ASSESSMENT_VERSION,
            ),
            counts={},
        )
        await session.commit()


async def _stage_fix_attempt(
    session_factory: async_sessionmaker[AsyncSession],
    *, finding_id: uuid.UUID, repository_id: uuid.UUID, candidate_fix_commit_sha: str, status: FixAttemptStatus,
) -> None:
    async with session_factory() as session:
        repo = FixAttemptRepository()
        model, _ = await repo.create(
            session, handoff_id=uuid.uuid4().hex, finding_id=finding_id, repository_id=repository_id,
            original_commit_sha=_ORIGINAL_SHA, candidate_fix_commit_sha=candidate_fix_commit_sha,
        )
        if status is not FixAttemptStatus.PENDING:
            if status is FixAttemptStatus.ERROR:
                await repo.mark_error(session, fix_attempt_id=model.id, error_message="test error")
            else:
                await repo.mark_result(
                    session, fix_attempt_id=model.id, status=status, deterministic_evidence=(),
                    executable_verification_outcome=None, remaining_issue_summary=None, limitations=(),
                )
        await session.commit()


# ---- No pull request / no review at all ----


async def test_unknown_pull_request_returns_none(session_factory: async_sessionmaker[AsyncSession]) -> None:
    repository_id = await _make_repository(session_factory, "acme/widgets")
    async with session_factory() as session:
        result = await MergeReadinessService().evaluate(
            session, repository_id=repository_id, pull_request_number=999
        )
    assert result is None


async def test_pull_request_never_reviewed_is_human_review_required(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    repository_id = await _make_repository(session_factory, "acme/widgets")
    await _make_pull_request(session_factory, repository_id=repository_id, number=1, head_sha=_HEAD_SHA)

    async with session_factory() as session:
        result = await MergeReadinessService().evaluate(
            session, repository_id=repository_id, pull_request_number=1
        )
    assert result is not None
    assert result.decision is MergeReadinessDecision.HUMAN_REVIEW_REQUIRED
    assert MergeReadinessReasonCode.STALE_REVIEW in result.reason_codes
    assert result.head_sha == _HEAD_SHA


async def test_review_at_old_sha_head_moved_is_stale(session_factory: async_sessionmaker[AsyncSession]) -> None:
    repository_id = await _make_repository(session_factory, "acme/widgets")
    pull_request_id = await _make_pull_request(
        session_factory, repository_id=repository_id, number=1, head_sha=_HEAD_SHA
    )
    await _stage_run_with_findings(
        session_factory, repository_id=repository_id, pull_request_id=pull_request_id, commit_sha=_ORIGINAL_SHA,
    )

    async with session_factory() as session:
        result = await MergeReadinessService().evaluate(
            session, repository_id=repository_id, pull_request_number=1
        )
    assert result is not None
    assert result.decision is MergeReadinessDecision.HUMAN_REVIEW_REQUIRED
    assert MergeReadinessReasonCode.STALE_REVIEW in result.reason_codes


# ---- Review incomplete (Part AT: absence of findings != review succeeded) ----


async def test_running_review_is_human_review_required(session_factory: async_sessionmaker[AsyncSession]) -> None:
    repository_id = await _make_repository(session_factory, "acme/widgets")
    pull_request_id = await _make_pull_request(
        session_factory, repository_id=repository_id, number=1, head_sha=_HEAD_SHA
    )
    await _stage_run_with_findings(
        session_factory, repository_id=repository_id, pull_request_id=pull_request_id, commit_sha=_HEAD_SHA,
        status=ReviewRunStatus.RUNNING,
    )

    async with session_factory() as session:
        result = await MergeReadinessService().evaluate(
            session, repository_id=repository_id, pull_request_number=1
        )
    assert result is not None
    assert result.decision is MergeReadinessDecision.HUMAN_REVIEW_REQUIRED
    assert MergeReadinessReasonCode.REVIEW_INCOMPLETE in result.reason_codes


async def test_failed_review_with_zero_findings_is_not_ready(session_factory: async_sessionmaker[AsyncSession]) -> None:
    # The critical distinction (Part AT): zero findings from a FAILED run
    # must never look like a clean pass.
    repository_id = await _make_repository(session_factory, "acme/widgets")
    pull_request_id = await _make_pull_request(
        session_factory, repository_id=repository_id, number=1, head_sha=_HEAD_SHA
    )
    await _stage_run_with_findings(
        session_factory, repository_id=repository_id, pull_request_id=pull_request_id, commit_sha=_HEAD_SHA,
        status=ReviewRunStatus.FAILED,
    )

    async with session_factory() as session:
        result = await MergeReadinessService().evaluate(
            session, repository_id=repository_id, pull_request_number=1
        )
    assert result is not None
    assert result.decision is MergeReadinessDecision.HUMAN_REVIEW_REQUIRED
    assert MergeReadinessReasonCode.REVIEW_INCOMPLETE in result.reason_codes


async def test_partial_review_is_human_review_required(session_factory: async_sessionmaker[AsyncSession]) -> None:
    repository_id = await _make_repository(session_factory, "acme/widgets")
    pull_request_id = await _make_pull_request(
        session_factory, repository_id=repository_id, number=1, head_sha=_HEAD_SHA
    )
    await _stage_run_with_findings(
        session_factory, repository_id=repository_id, pull_request_id=pull_request_id, commit_sha=_HEAD_SHA,
        status=ReviewRunStatus.PARTIAL,
    )

    async with session_factory() as session:
        result = await MergeReadinessService().evaluate(
            session, repository_id=repository_id, pull_request_number=1
        )
    assert result is not None
    assert result.decision is MergeReadinessDecision.HUMAN_REVIEW_REQUIRED
    assert MergeReadinessReasonCode.REVIEW_INCOMPLETE in result.reason_codes


# ---- Clean succeeded review ----


async def test_succeeded_review_with_no_candidates_is_ready(session_factory: async_sessionmaker[AsyncSession]) -> None:
    repository_id = await _make_repository(session_factory, "acme/widgets")
    pull_request_id = await _make_pull_request(
        session_factory, repository_id=repository_id, number=1, head_sha=_HEAD_SHA
    )
    await _stage_run_with_findings(
        session_factory, repository_id=repository_id, pull_request_id=pull_request_id, commit_sha=_HEAD_SHA,
    )

    async with session_factory() as session:
        result = await MergeReadinessService().evaluate(
            session, repository_id=repository_id, pull_request_number=1
        )
    assert result is not None
    assert result.decision is MergeReadinessDecision.READY
    assert result.reason_codes == (MergeReadinessReasonCode.NO_UNRESOLVED_EVIDENCE,)
    assert result.finding_ids == ()


async def test_low_severity_finding_alone_is_ready(session_factory: async_sessionmaker[AsyncSession]) -> None:
    repository_id = await _make_repository(session_factory, "acme/widgets")
    pull_request_id = await _make_pull_request(
        session_factory, repository_id=repository_id, number=1, head_sha=_HEAD_SHA
    )
    await _stage_run_with_findings(
        session_factory, repository_id=repository_id, pull_request_id=pull_request_id, commit_sha=_HEAD_SHA,
        findings=[(Severity.LOW, FindingCategory.STYLE)],
    )

    async with session_factory() as session:
        result = await MergeReadinessService().evaluate(
            session, repository_id=repository_id, pull_request_number=1
        )
    assert result is not None
    assert result.decision is MergeReadinessDecision.READY


async def test_medium_non_security_finding_alone_is_ready(session_factory: async_sessionmaker[AsyncSession]) -> None:
    repository_id = await _make_repository(session_factory, "acme/widgets")
    pull_request_id = await _make_pull_request(
        session_factory, repository_id=repository_id, number=1, head_sha=_HEAD_SHA
    )
    await _stage_run_with_findings(
        session_factory, repository_id=repository_id, pull_request_id=pull_request_id, commit_sha=_HEAD_SHA,
        findings=[(Severity.MEDIUM, FindingCategory.MAINTAINABILITY)],
    )

    async with session_factory() as session:
        result = await MergeReadinessService().evaluate(
            session, repository_id=repository_id, pull_request_number=1
        )
    assert result is not None
    assert result.decision is MergeReadinessDecision.READY


# ---- Blocking findings ----


async def test_unresolved_high_finding_is_blocked(session_factory: async_sessionmaker[AsyncSession]) -> None:
    repository_id = await _make_repository(session_factory, "acme/widgets")
    pull_request_id = await _make_pull_request(
        session_factory, repository_id=repository_id, number=1, head_sha=_HEAD_SHA
    )
    _run_id, finding_ids = await _stage_run_with_findings(
        session_factory, repository_id=repository_id, pull_request_id=pull_request_id, commit_sha=_HEAD_SHA,
        findings=[(Severity.HIGH, FindingCategory.CORRECTNESS)],
    )

    async with session_factory() as session:
        result = await MergeReadinessService().evaluate(
            session, repository_id=repository_id, pull_request_number=1
        )
    assert result is not None
    assert result.decision is MergeReadinessDecision.BLOCKED
    assert result.reason_codes == (MergeReadinessReasonCode.UNRESOLVED_BLOCKING_FINDING,)
    assert result.finding_ids == tuple(finding_ids)


async def test_unresolved_critical_finding_is_blocked(session_factory: async_sessionmaker[AsyncSession]) -> None:
    repository_id = await _make_repository(session_factory, "acme/widgets")
    pull_request_id = await _make_pull_request(
        session_factory, repository_id=repository_id, number=1, head_sha=_HEAD_SHA
    )
    await _stage_run_with_findings(
        session_factory, repository_id=repository_id, pull_request_id=pull_request_id, commit_sha=_HEAD_SHA,
        findings=[(Severity.CRITICAL, FindingCategory.SECURITY)],
    )

    async with session_factory() as session:
        result = await MergeReadinessService().evaluate(
            session, repository_id=repository_id, pull_request_number=1
        )
    assert result is not None
    assert result.decision is MergeReadinessDecision.BLOCKED


async def test_high_confidence_security_auth_bypass_is_blocked(session_factory: async_sessionmaker[AsyncSession]) -> None:
    # Part AE: security blocks on evidence + severity, not category alone.
    repository_id = await _make_repository(session_factory, "acme/widgets")
    pull_request_id = await _make_pull_request(
        session_factory, repository_id=repository_id, number=1, head_sha=_HEAD_SHA
    )
    await _stage_run_with_findings(
        session_factory, repository_id=repository_id, pull_request_id=pull_request_id, commit_sha=_HEAD_SHA,
        findings=[(Severity.HIGH, FindingCategory.SECURITY)],
    )

    async with session_factory() as session:
        result = await MergeReadinessService().evaluate(
            session, repository_id=repository_id, pull_request_number=1
        )
    assert result is not None
    assert result.decision is MergeReadinessDecision.BLOCKED


async def test_medium_security_finding_is_human_review_required_not_blocked(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    repository_id = await _make_repository(session_factory, "acme/widgets")
    pull_request_id = await _make_pull_request(
        session_factory, repository_id=repository_id, number=1, head_sha=_HEAD_SHA
    )
    await _stage_run_with_findings(
        session_factory, repository_id=repository_id, pull_request_id=pull_request_id, commit_sha=_HEAD_SHA,
        findings=[(Severity.MEDIUM, FindingCategory.SECURITY)],
    )

    async with session_factory() as session:
        result = await MergeReadinessService().evaluate(
            session, repository_id=repository_id, pull_request_number=1
        )
    assert result is not None
    assert result.decision is MergeReadinessDecision.HUMAN_REVIEW_REQUIRED
    assert MergeReadinessReasonCode.HIGH_IMPACT_INCONCLUSIVE in result.reason_codes


# ---- Dismissed / feedback-resolved findings ----


async def test_dismissed_high_finding_is_not_blocking(session_factory: async_sessionmaker[AsyncSession]) -> None:
    repository_id = await _make_repository(session_factory, "acme/widgets")
    pull_request_id = await _make_pull_request(
        session_factory, repository_id=repository_id, number=1, head_sha=_HEAD_SHA
    )
    _run_id, finding_ids = await _stage_run_with_findings(
        session_factory, repository_id=repository_id, pull_request_id=pull_request_id, commit_sha=_HEAD_SHA,
        findings=[(Severity.HIGH, FindingCategory.CORRECTNESS)],
    )
    await _dismiss_finding(session_factory, finding_id=finding_ids[0])

    async with session_factory() as session:
        result = await MergeReadinessService().evaluate(
            session, repository_id=repository_id, pull_request_number=1
        )
    assert result is not None
    assert result.decision is MergeReadinessDecision.READY


# ---- FixAttempt interaction (Part AF) ----


async def test_blocking_finding_fixed_at_exact_head_clears_it(session_factory: async_sessionmaker[AsyncSession]) -> None:
    repository_id = await _make_repository(session_factory, "acme/widgets")
    pull_request_id = await _make_pull_request(
        session_factory, repository_id=repository_id, number=1, head_sha=_HEAD_SHA
    )
    _run_id, finding_ids = await _stage_run_with_findings(
        session_factory, repository_id=repository_id, pull_request_id=pull_request_id, commit_sha=_HEAD_SHA,
        findings=[(Severity.HIGH, FindingCategory.CORRECTNESS)],
    )
    await _stage_fix_attempt(
        session_factory, finding_id=finding_ids[0], repository_id=repository_id,
        candidate_fix_commit_sha=_HEAD_SHA, status=FixAttemptStatus.FIXED,
    )

    async with session_factory() as session:
        result = await MergeReadinessService().evaluate(
            session, repository_id=repository_id, pull_request_number=1
        )
    assert result is not None
    assert result.decision is MergeReadinessDecision.READY


async def test_fixed_at_old_sha_does_not_clear_current_head_finding(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # Part AF's own explicit instruction: FixAttempt for old SHA must
    # never clear a current-head finding.
    repository_id = await _make_repository(session_factory, "acme/widgets")
    pull_request_id = await _make_pull_request(
        session_factory, repository_id=repository_id, number=1, head_sha=_HEAD_SHA
    )
    _run_id, finding_ids = await _stage_run_with_findings(
        session_factory, repository_id=repository_id, pull_request_id=pull_request_id, commit_sha=_HEAD_SHA,
        findings=[(Severity.HIGH, FindingCategory.CORRECTNESS)],
    )
    await _stage_fix_attempt(
        session_factory, finding_id=finding_ids[0], repository_id=repository_id,
        candidate_fix_commit_sha="c" * 40,  # a different, non-current SHA
        status=FixAttemptStatus.FIXED,
    )

    async with session_factory() as session:
        result = await MergeReadinessService().evaluate(
            session, repository_id=repository_id, pull_request_number=1
        )
    assert result is not None
    assert result.decision is MergeReadinessDecision.BLOCKED


async def test_inconclusive_fix_at_exact_head_is_human_review_required(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    repository_id = await _make_repository(session_factory, "acme/widgets")
    pull_request_id = await _make_pull_request(
        session_factory, repository_id=repository_id, number=1, head_sha=_HEAD_SHA
    )
    _run_id, finding_ids = await _stage_run_with_findings(
        session_factory, repository_id=repository_id, pull_request_id=pull_request_id, commit_sha=_HEAD_SHA,
        findings=[(Severity.HIGH, FindingCategory.CORRECTNESS)],
    )
    await _stage_fix_attempt(
        session_factory, finding_id=finding_ids[0], repository_id=repository_id,
        candidate_fix_commit_sha=_HEAD_SHA, status=FixAttemptStatus.INCONCLUSIVE,
    )

    async with session_factory() as session:
        result = await MergeReadinessService().evaluate(
            session, repository_id=repository_id, pull_request_number=1
        )
    assert result is not None
    assert result.decision is MergeReadinessDecision.HUMAN_REVIEW_REQUIRED
    assert MergeReadinessReasonCode.HIGH_IMPACT_INCONCLUSIVE in result.reason_codes


async def test_stale_fix_attempt_does_not_clear_blocking_finding(session_factory: async_sessionmaker[AsyncSession]) -> None:
    repository_id = await _make_repository(session_factory, "acme/widgets")
    pull_request_id = await _make_pull_request(
        session_factory, repository_id=repository_id, number=1, head_sha=_HEAD_SHA
    )
    _run_id, finding_ids = await _stage_run_with_findings(
        session_factory, repository_id=repository_id, pull_request_id=pull_request_id, commit_sha=_HEAD_SHA,
        findings=[(Severity.HIGH, FindingCategory.CORRECTNESS)],
    )
    await _stage_fix_attempt(
        session_factory, finding_id=finding_ids[0], repository_id=repository_id,
        candidate_fix_commit_sha=_HEAD_SHA, status=FixAttemptStatus.STALE,
    )

    async with session_factory() as session:
        result = await MergeReadinessService().evaluate(
            session, repository_id=repository_id, pull_request_number=1
        )
    assert result is not None
    assert result.decision is MergeReadinessDecision.BLOCKED


async def test_error_fix_attempt_does_not_clear_blocking_finding(session_factory: async_sessionmaker[AsyncSession]) -> None:
    repository_id = await _make_repository(session_factory, "acme/widgets")
    pull_request_id = await _make_pull_request(
        session_factory, repository_id=repository_id, number=1, head_sha=_HEAD_SHA
    )
    _run_id, finding_ids = await _stage_run_with_findings(
        session_factory, repository_id=repository_id, pull_request_id=pull_request_id, commit_sha=_HEAD_SHA,
        findings=[(Severity.HIGH, FindingCategory.CORRECTNESS)],
    )
    await _stage_fix_attempt(
        session_factory, finding_id=finding_ids[0], repository_id=repository_id,
        candidate_fix_commit_sha=_HEAD_SHA, status=FixAttemptStatus.ERROR,
    )

    async with session_factory() as session:
        result = await MergeReadinessService().evaluate(
            session, repository_id=repository_id, pull_request_number=1
        )
    assert result is not None
    assert result.decision is MergeReadinessDecision.BLOCKED


async def test_still_present_fix_attempt_reinforces_blocked(session_factory: async_sessionmaker[AsyncSession]) -> None:
    repository_id = await _make_repository(session_factory, "acme/widgets")
    pull_request_id = await _make_pull_request(
        session_factory, repository_id=repository_id, number=1, head_sha=_HEAD_SHA
    )
    _run_id, finding_ids = await _stage_run_with_findings(
        session_factory, repository_id=repository_id, pull_request_id=pull_request_id, commit_sha=_HEAD_SHA,
        findings=[(Severity.HIGH, FindingCategory.CORRECTNESS)],
    )
    await _stage_fix_attempt(
        session_factory, finding_id=finding_ids[0], repository_id=repository_id,
        candidate_fix_commit_sha=_HEAD_SHA, status=FixAttemptStatus.STILL_PRESENT,
    )

    async with session_factory() as session:
        result = await MergeReadinessService().evaluate(
            session, repository_id=repository_id, pull_request_number=1
        )
    assert result is not None
    assert result.decision is MergeReadinessDecision.BLOCKED


async def test_pending_fix_attempt_is_human_review_required(session_factory: async_sessionmaker[AsyncSession]) -> None:
    repository_id = await _make_repository(session_factory, "acme/widgets")
    pull_request_id = await _make_pull_request(
        session_factory, repository_id=repository_id, number=1, head_sha=_HEAD_SHA
    )
    _run_id, finding_ids = await _stage_run_with_findings(
        session_factory, repository_id=repository_id, pull_request_id=pull_request_id, commit_sha=_HEAD_SHA,
        findings=[(Severity.HIGH, FindingCategory.CORRECTNESS)],
    )
    await _stage_fix_attempt(
        session_factory, finding_id=finding_ids[0], repository_id=repository_id,
        candidate_fix_commit_sha=_HEAD_SHA, status=FixAttemptStatus.PENDING,
    )

    async with session_factory() as session:
        result = await MergeReadinessService().evaluate(
            session, repository_id=repository_id, pull_request_number=1
        )
    assert result is not None
    assert result.decision is MergeReadinessDecision.HUMAN_REVIEW_REQUIRED
    assert MergeReadinessReasonCode.BLOCKING_FIX_NOT_VERIFIED in result.reason_codes


# ---- Determinism, exact-head isolation, no numeric score ----


async def test_decision_is_deterministic_for_identical_evidence(session_factory: async_sessionmaker[AsyncSession]) -> None:
    repository_id = await _make_repository(session_factory, "acme/widgets")
    pull_request_id = await _make_pull_request(
        session_factory, repository_id=repository_id, number=1, head_sha=_HEAD_SHA
    )
    await _stage_run_with_findings(
        session_factory, repository_id=repository_id, pull_request_id=pull_request_id, commit_sha=_HEAD_SHA,
        findings=[(Severity.HIGH, FindingCategory.CORRECTNESS)],
    )

    async with session_factory() as session:
        result_a = await MergeReadinessService().evaluate(
            session, repository_id=repository_id, pull_request_number=1
        )
    async with session_factory() as session:
        result_b = await MergeReadinessService().evaluate(
            session, repository_id=repository_id, pull_request_number=1
        )
    assert result_a is not None and result_b is not None
    assert result_a.decision == result_b.decision
    assert result_a.reason_codes == result_b.reason_codes


async def test_old_sha_readiness_never_reused_for_new_sha(session_factory: async_sessionmaker[AsyncSession]) -> None:
    repository_id = await _make_repository(session_factory, "acme/widgets")
    pull_request_id = await _make_pull_request(
        session_factory, repository_id=repository_id, number=1, head_sha=_ORIGINAL_SHA
    )
    await _stage_run_with_findings(
        session_factory, repository_id=repository_id, pull_request_id=pull_request_id, commit_sha=_ORIGINAL_SHA,
    )
    async with session_factory() as session:
        old_result = await MergeReadinessService().evaluate(
            session, repository_id=repository_id, pull_request_number=1
        )
    assert old_result is not None
    assert old_result.decision is MergeReadinessDecision.READY
    assert old_result.head_sha == _ORIGINAL_SHA

    # Head moves to a new SHA with no review yet at that head.
    async with session_factory() as session:
        pr_repo = PullRequestRepository()
        pr = await pr_repo.get_by_repository_and_number(session, repository_id=repository_id, github_pr_number=1)
        assert pr is not None
        await pr_repo.upsert(
            session, repository_id=repository_id, github_pr_number=1, title="t", author="a",
            base_sha=_ORIGINAL_SHA, head_sha=_HEAD_SHA, state="open",
        )
        await session.commit()

    async with session_factory() as session:
        new_result = await MergeReadinessService().evaluate(
            session, repository_id=repository_id, pull_request_number=1
        )
    assert new_result is not None
    assert new_result.decision is MergeReadinessDecision.HUMAN_REVIEW_REQUIRED
    assert new_result.head_sha == _HEAD_SHA
    assert new_result.head_sha != old_result.head_sha


def test_result_has_no_numeric_score_field() -> None:
    from dataclasses import fields

    from patchfrog.merge_readiness.domain import MergeReadinessResult

    field_names = {f.name for f in fields(MergeReadinessResult)}
    forbidden = {"score", "risk_score", "merge_score", "confidence_percentage", "readiness_percentage"}
    assert field_names & forbidden == set()


def test_decision_enum_has_exactly_three_members() -> None:
    assert {d.value for d in MergeReadinessDecision} == {"ready", "blocked", "human_review_required"}
