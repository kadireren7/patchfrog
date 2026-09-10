"""Merge Readiness service -- Milestone V.

Deterministic over PatchFrog's own already-persisted evidence, **never**
an LLM call asking "should this PR merge" -- see module docstring in
:mod:`patchfrog.merge_readiness.domain` for the full governing rule.

**Decision precedence** (highest first):

1. No review run exists at the PR's exact current head at all (never
   reviewed at this SHA, or the head moved since the last review) ->
   ``HUMAN_REVIEW_REQUIRED`` / ``STALE_REVIEW``.
2. A review run exists at this exact head but did not finish
   (``RUNNING``) or a required step failed (``FAILED``/``PARTIAL``) ->
   ``HUMAN_REVIEW_REQUIRED`` / ``REVIEW_INCOMPLETE``. This is the
   critical distinction Part AT of this milestone's spec calls out
   explicitly: "no findings because the review didn't run" must never
   look like "no findings because it ran and found none."
3. At least one accepted, still-open CRITICAL/HIGH finding with no
   same-exact-head ``FIXED`` :class:`~patchfrog.fix_verification.domain.FixAttempt`
   and no human dismissal (``ResolutionState.CLOSED``) ->
   ``BLOCKED`` / ``UNRESOLVED_BLOCKING_FINDING``.
4. A would-be-blocking finding's only exact-head fix evidence is
   ``INCONCLUSIVE``, ``PENDING``, or ``VERIFYING`` (not yet a decisive
   verdict), or a MEDIUM-severity SECURITY finding remains open ->
   ``HUMAN_REVIEW_REQUIRED`` / ``HIGH_IMPACT_INCONCLUSIVE`` or
   ``BLOCKING_FIX_NOT_VERIFIED``.
5. Otherwise -> ``READY`` / ``NO_UNRESOLVED_EVIDENCE``.

**What counts as "still open"**: every :class:`~patchfrog.persistence.models.review.AIFindingModel`
row for the exact-head run -- these already survived validation, the
critic, confidence aggregation, and dedup (see that model's own
docstring), so every row is real accepted evidence regardless of whether
it was actually *published* inline vs. summarized (publication
disposition governs GitHub comment-volume budgeting, not evidence
truth) -- **unless** a :class:`~patchfrog.feedback.domain.FeedbackAssessment`
exists for it with ``resolution_signal == ResolutionState.CLOSED``
(a human has explicitly dismissed/resolved it), or a
:class:`~patchfrog.fix_verification.domain.FixAttempt` for that exact
finding, at ``candidate_fix_commit_sha == this PR's current head_sha``
(never any other SHA -- Part AF's own explicit instruction), resolved
``FIXED``.

**Severity/category policy** (Part AD/AE, precision over recall): a
finding does not automatically block merely by category -- a LOW/INFO
finding, or a MEDIUM finding outside SECURITY, never blocks or escalates
on its own. J-R Intelligence evidence (Change/Contract/Intent/Test/
Historical/Repository-Learnings/Trajectory/Cross-PR/Cross-Repo) is never
consulted here at all: none of it is finding-specific persisted evidence
(see ``validation/model_router_merge_readiness/latest-summary.md``
section 2) -- it can only ever inform read-only evidence exposed
elsewhere (T's ``FindingHandoff``), never a Merge Readiness blocker or
escalation by itself.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from patchfrog.analysis.domain import FindingCategory, Severity
from patchfrog.feedback.domain import FEEDBACK_ASSESSMENT_VERSION, ResolutionState
from patchfrog.fix_verification.domain import FixAttemptStatus
from patchfrog.merge_readiness.domain import (
    MergeReadinessDecision,
    MergeReadinessReasonCode,
    MergeReadinessResult,
)
from patchfrog.persistence.models.review import AIFindingModel
from patchfrog.persistence.repositories.ai_finding import AIFindingRepository
from patchfrog.persistence.repositories.feedback import FeedbackAssessmentRepository
from patchfrog.persistence.repositories.fix_attempt import FixAttemptRepository
from patchfrog.persistence.repositories.pull_request import PullRequestRepository
from patchfrog.persistence.repositories.review_run import ReviewRunRepository
from patchfrog.review.domain import ReviewRunStatus

_BLOCKING_SEVERITIES = frozenset({Severity.CRITICAL, Severity.HIGH})
#: A completed-but-undecided fix verification at the exact same head --
#: not still-blocking proof, not a clearance.
_INCONCLUSIVE_FIX_STATUSES = frozenset({FixAttemptStatus.INCONCLUSIVE})
#: A fix verification still in flight at the exact same head -- not yet
#: any kind of evidence at all.
_IN_PROGRESS_FIX_STATUSES = frozenset({FixAttemptStatus.PENDING, FixAttemptStatus.VERIFYING})


class MergeReadinessService:
    def __init__(
        self,
        *,
        pull_request_repo: PullRequestRepository | None = None,
        run_repo: ReviewRunRepository | None = None,
        finding_repo: AIFindingRepository | None = None,
        assessment_repo: FeedbackAssessmentRepository | None = None,
        fix_attempt_repo: FixAttemptRepository | None = None,
    ) -> None:
        self._pull_requests = pull_request_repo or PullRequestRepository()
        self._runs = run_repo or ReviewRunRepository()
        self._findings = finding_repo or AIFindingRepository()
        self._assessments = assessment_repo or FeedbackAssessmentRepository()
        self._fix_attempts = fix_attempt_repo or FixAttemptRepository()

    async def evaluate(
        self, session: AsyncSession, *, repository_id: UUID, pull_request_number: int
    ) -> MergeReadinessResult | None:
        """``None`` only when no pull request record exists at all for
        ``(repository_id, pull_request_number)`` -- distinct from every
        other case, which always returns a real result (even a stale or
        incomplete one is a real, reportable decision)."""

        pull_request = await self._pull_requests.get_by_repository_and_number(
            session, repository_id=repository_id, github_pr_number=pull_request_number
        )
        if pull_request is None:
            return None

        run = await self._runs.get_latest_for_pull_request(session, pull_request_id=pull_request.id)
        if run is None or run.commit_sha != pull_request.head_sha:
            return MergeReadinessResult(
                decision=MergeReadinessDecision.HUMAN_REVIEW_REQUIRED,
                reason_codes=(MergeReadinessReasonCode.STALE_REVIEW,),
                repository_id=repository_id,
                pull_request_number=pull_request_number,
                review_run_id=None,
                head_sha=pull_request.head_sha,
                finding_ids=(),
                limitations=("no review run exists at this exact PR head",),
            )

        if run.status is not ReviewRunStatus.SUCCEEDED:
            return MergeReadinessResult(
                decision=MergeReadinessDecision.HUMAN_REVIEW_REQUIRED,
                reason_codes=(MergeReadinessReasonCode.REVIEW_INCOMPLETE,),
                repository_id=repository_id,
                pull_request_number=pull_request_number,
                review_run_id=run.id,
                head_sha=pull_request.head_sha,
                finding_ids=(),
                limitations=(
                    f"the review run at this exact head has status={run.status.value!r}, not succeeded -- "
                    "absence of findings here would not mean absence of the underlying condition",
                ),
            )

        findings = await self._findings.list_for_run(session, review_run_id=run.id)
        if not findings:
            return MergeReadinessResult(
                decision=MergeReadinessDecision.READY,
                reason_codes=(MergeReadinessReasonCode.NO_UNRESOLVED_EVIDENCE,),
                repository_id=repository_id,
                pull_request_number=pull_request_number,
                review_run_id=run.id,
                head_sha=pull_request.head_sha,
                finding_ids=(),
                limitations=(),
            )

        assessments = await self._assessments.list_for_findings(
            session, finding_ids=[f.id for f in findings], assessment_version=FEEDBACK_ASSESSMENT_VERSION,
        )
        closed_finding_ids = {
            a.finding_id for a in assessments if a.resolution_signal is ResolutionState.CLOSED
        }

        blocking: list[UUID] = []
        escalating_inconclusive: list[UUID] = []
        escalating_in_progress: list[UUID] = []
        escalating_medium_security: list[UUID] = []

        for finding in findings:
            if finding.id in closed_finding_ids:
                continue

            same_head_status = await self._same_head_fix_status(
                session, finding=finding, head_sha=pull_request.head_sha
            )
            if same_head_status is FixAttemptStatus.FIXED:
                continue

            if finding.severity in _BLOCKING_SEVERITIES:
                if same_head_status in _INCONCLUSIVE_FIX_STATUSES:
                    escalating_inconclusive.append(finding.id)
                elif same_head_status in _IN_PROGRESS_FIX_STATUSES:
                    escalating_in_progress.append(finding.id)
                else:
                    blocking.append(finding.id)
            elif finding.severity is Severity.MEDIUM and finding.category is FindingCategory.SECURITY:
                escalating_medium_security.append(finding.id)
            # LOW/INFO, or MEDIUM outside SECURITY: never blocking or
            # escalating on their own (Part AD/AE).

        if blocking:
            return MergeReadinessResult(
                decision=MergeReadinessDecision.BLOCKED,
                reason_codes=(MergeReadinessReasonCode.UNRESOLVED_BLOCKING_FINDING,),
                repository_id=repository_id,
                pull_request_number=pull_request_number,
                review_run_id=run.id,
                head_sha=pull_request.head_sha,
                finding_ids=tuple(blocking),
                limitations=(),
            )

        escalating = escalating_inconclusive + escalating_in_progress + escalating_medium_security
        if escalating:
            reasons: list[MergeReadinessReasonCode] = []
            if escalating_inconclusive or escalating_medium_security:
                reasons.append(MergeReadinessReasonCode.HIGH_IMPACT_INCONCLUSIVE)
            if escalating_in_progress:
                reasons.append(MergeReadinessReasonCode.BLOCKING_FIX_NOT_VERIFIED)
            return MergeReadinessResult(
                decision=MergeReadinessDecision.HUMAN_REVIEW_REQUIRED,
                reason_codes=tuple(reasons),
                repository_id=repository_id,
                pull_request_number=pull_request_number,
                review_run_id=run.id,
                head_sha=pull_request.head_sha,
                finding_ids=tuple(escalating),
                limitations=(),
            )

        return MergeReadinessResult(
            decision=MergeReadinessDecision.READY,
            reason_codes=(MergeReadinessReasonCode.NO_UNRESOLVED_EVIDENCE,),
            repository_id=repository_id,
            pull_request_number=pull_request_number,
            review_run_id=run.id,
            head_sha=pull_request.head_sha,
            finding_ids=(),
            limitations=(),
        )

    async def _same_head_fix_status(
        self, session: AsyncSession, *, finding: AIFindingModel, head_sha: str
    ) -> FixAttemptStatus | None:
        attempts = await self._fix_attempts.list_for_finding(session, finding_id=finding.id)
        same_head = [a for a in attempts if a.candidate_fix_commit_sha == head_sha]
        if not same_head:
            return None
        # STALE/ERROR attempts never clear or escalate anything (Part
        # AF: "STALE: cannot clear anything. ERROR: cannot clear
        # anything.") -- filtered out entirely rather than treated as a
        # signal of any kind.
        meaningful = [
            a for a in same_head
            if a.status not in (FixAttemptStatus.STALE, FixAttemptStatus.ERROR)
        ]
        if not meaningful:
            return None
        # A same-head FIXED verdict wins outright if any exists (the
        # decisive, positive signal); otherwise the most recently
        # created meaningful attempt's status governs.
        if any(a.status is FixAttemptStatus.FIXED for a in meaningful):
            return FixAttemptStatus.FIXED
        latest = max(meaningful, key=lambda a: a.created_at)
        return latest.status
