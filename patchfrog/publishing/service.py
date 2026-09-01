"""Orchestrates one GitHub review-publication attempt end to end.

    completed review_run_id
    -> load repository / pull request identity
    -> live GitHub read (current head SHA, diff) -- never cached
    -> deterministic planner (patchfrog.publishing.planner)
    -> [DRY_RUN: stop here, no write]
    -> [PUBLISH: durable PUBLISHING marker -> GitHub write -> PUBLISHED]
    -> persisted ReviewPublicationModel + ReviewPublicationCommentModel rows

Publishing a completed review result never invokes an LLM again --
:class:`ReviewPublicationService` only ever reads already-persisted Phase 5
``ai_findings`` rows, plus (see
:func:`patchfrog.publishing.queries.get_current_active_findings`) any
Phase 7 (:mod:`patchfrog.review_memory`) finding that was zero-AI-call
carried forward to this exact run and never actually published before.
Review generation is an AI operation; publishing is a deterministic side effect.
The two are deliberately separate Celery tasks (see
:mod:`apps.worker.tasks.publish_review` vs
:mod:`apps.worker.tasks.review_pull_request`) precisely so publishing can
be retried independently of review generation.

GitHub and PostgreSQL cannot share one transaction (section 23 of the
Phase 6 spec) -- this service never pretends otherwise. The durable
sequence is: (1) lock the ``(review_run_id, mode)`` identity and commit a
``PUBLISHING`` row *before* the GitHub write is attempted; (2) perform the
GitHub write with no lock held; (3) re-lock the same identity and commit
``PUBLISHED``/``FAILED``. If the process crashes between (2) and (3), a
later attempt finds the ``PUBLISHING`` row and reconciles against GitHub
by marker (see :mod:`patchfrog.publishing.marker`) rather than writing
again -- see :meth:`ReviewPublicationRepository.get_or_create_attempt`
for the full state machine.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from patchfrog.diff.parser import build_diff_file
from patchfrog.domain.github_review import (
    GitHubDiffSide,
    GitHubReviewCommentInput,
    GitHubReviewEvent,
)
from patchfrog.domain.pull_request import PullRequestRef
from patchfrog.persistence.models.publishing import ReviewPublicationModel
from patchfrog.persistence.models.pull_request import PullRequestModel
from patchfrog.persistence.models.repository import RepositoryModel
from patchfrog.persistence.models.review import ReviewRunModel
from patchfrog.persistence.repositories.review_publication import (
    PublicationAttemptOutcome,
    ReviewPublicationRepository,
)
from patchfrog.persistence.repositories.review_publication_comment import (
    ReviewPublicationCommentRepository,
)
from patchfrog.publishing.config import PublicationConfig
from patchfrog.publishing.domain import (
    DiffSide,
    ReviewInputSnapshot,
    ReviewPublicationComment,
    ReviewPublicationMode,
    ReviewPublicationPlan,
    ReviewPublicationResult,
    ReviewPublicationStatus,
)
from patchfrog.publishing.errors import classify_github_exception
from patchfrog.publishing.github_publisher import ReviewPublisher
from patchfrog.publishing.planner import PublicationPlanner
from patchfrog.publishing.queries import get_current_active_findings

logger = structlog.get_logger(__name__)


class ReviewNotFoundError(RuntimeError):
    """No ``review_runs`` row with the given id exists."""


class ReviewRunNotAssociatedWithPullRequestError(RuntimeError):
    """Publishing requires a known pull request -- a review run created
    without one (e.g. a CLI local-diff dry run) cannot be published."""


_DIFF_SIDE_TO_GITHUB = {DiffSide.OLD: GitHubDiffSide.LEFT, DiffSide.NEW: GitHubDiffSide.RIGHT}


class ReviewPublicationService:
    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        publisher: ReviewPublisher,
        planner: PublicationPlanner | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._publisher = publisher
        self._planner = planner or PublicationPlanner()
        self._publication_repo = ReviewPublicationRepository()
        self._comment_repo = ReviewPublicationCommentRepository()

    async def publish(
        self,
        *,
        review_run_id: uuid.UUID,
        mode: ReviewPublicationMode,
        config: PublicationConfig | None = None,
        already_reported_finding_ids: frozenset[uuid.UUID] = frozenset(),
    ) -> ReviewPublicationResult:
        """``already_reported_finding_ids`` is an optional caller-supplied
        addition to the ``ALREADY_REPORTED`` suppression set -- the
        Phase 7 (:mod:`patchfrog.review_memory`) contribution to that set
        is always computed internally, from ``review_run_id`` alone, via
        :func:`patchfrog.publishing.queries.get_current_active_findings`
        (which also supplies any safely carried-forward finding that has
        never actually been published, merged into ``findings`` below).
        A publish retry/redelivery therefore always recomputes both
        correctly with no dependency on anything the review task passed
        through in-process."""

        config = config or PublicationConfig()

        async with self._session_factory() as session:
            run = await session.get(ReviewRunModel, review_run_id)
            if run is None:
                raise ReviewNotFoundError(f"no review run with id {review_run_id}")
            if run.pull_request_id is None:
                raise ReviewRunNotAssociatedWithPullRequestError(
                    f"review run {review_run_id} has no associated pull request; publishing requires one"
                )
            repository = await session.get(RepositoryModel, run.repository_id)
            pull_request = await session.get(PullRequestModel, run.pull_request_id)
            assert repository is not None and pull_request is not None  # FK-enforced

            findings, memory_already_reported_finding_ids = await get_current_active_findings(
                session, review_run_id=review_run_id
            )
            already_reported_finding_ids = already_reported_finding_ids | memory_already_reported_finding_ids

        ref = PullRequestRef(owner=repository.owner, repository=repository.name, number=pull_request.github_pr_number)
        snapshot = ReviewInputSnapshot(
            repository_id=repository.id,
            repository_full_name=repository.full_name,
            pull_request_number=pull_request.github_pr_number,
            review_run_id=review_run_id,
            head_sha=run.commit_sha,
        )

        initial_status = (
            ReviewPublicationStatus.PUBLISHING if mode is ReviewPublicationMode.PUBLISH else ReviewPublicationStatus.DRY_RUN
        )
        policy_fingerprint = config.fingerprint()

        async with self._session_factory() as session:
            attempt, outcome = await self._publication_repo.get_or_create_attempt(
                session,
                review_run_id=review_run_id,
                repository_id=repository.id,
                pull_request_id=pull_request.id,
                pull_request_number=pull_request.github_pr_number,
                base_sha=pull_request.base_sha,
                head_sha=run.commit_sha,
                mode=mode,
                publication_policy_fingerprint=policy_fingerprint,
                initial_status=initial_status,
            )
            await session.commit()

        if outcome is PublicationAttemptOutcome.ALREADY_PUBLISHED:
            logger.info("review_publish_reconciled", publication_id=str(attempt.id), reason="already_published")
            return self._result_from_model(attempt, reconciled=True, errors=())

        if outcome is PublicationAttemptOutcome.IN_PROGRESS_ELSEWHERE:
            logger.info("review_publish_skipped_in_progress_elsewhere", publication_id=str(attempt.id))
            return self._result_from_model(attempt, reconciled=False, errors=("another publish attempt is in progress",))

        if outcome is PublicationAttemptOutcome.RECONCILE_NEEDED:
            found = await self._publisher.find_patchfrog_review(ref, publication_id=attempt.id)
            if found is not None:
                async with self._session_factory() as session:
                    published = await self._publication_repo.mark_published(
                        session,
                        publication_id=attempt.id,
                        github_review_id=found.id,
                        inline_count=attempt.inline_count,
                        summary_only_count=attempt.summary_only_count,
                        omitted_count=attempt.omitted_count,
                        reconciled=True,
                    )
                    await session.commit()
                logger.info("review_publish_reconciled", publication_id=str(attempt.id), reason="marker_found_on_github")
                return self._result_from_model(published, reconciled=True, errors=())

            async with self._session_factory() as session:
                await self._publication_repo.supersede_abandoned(session, publication_id=attempt.id)
                await session.commit()
            logger.info("review_publish_superseding_abandoned", old_publication_id=str(attempt.id))
            return await self.publish(review_run_id=review_run_id, mode=mode, config=config)

        # outcome is NEW -- proceed with planning.
        publication_id = attempt.id
        logger.info("review_publish_planned", publication_id=str(publication_id), review_run_id=str(review_run_id), mode=mode.value)

        try:
            current_head_sha = await self._publisher.get_head_sha(ref)
            changed_files = await self._publisher.get_pull_request_diff(ref)
        except Exception as exc:
            return await self._fail(publication_id, exc, skipped=len(findings))

        diff_files = [build_diff_file(f.path, f.patch) for f in changed_files]

        plan = self._planner.build_plan(
            publication_id=publication_id,
            snapshot=snapshot,
            findings=findings,
            changed_files=changed_files,
            diff_files=diff_files,
            config=config,
            mode=mode,
            current_head_sha=current_head_sha,
            already_reported_finding_ids=already_reported_finding_ids,
        )

        await self._persist_plan_comments(publication_id, plan)

        if plan.status is ReviewPublicationStatus.STALE:
            logger.info("review_publish_stale", publication_id=str(publication_id), reason=plan.reason)
            return await self._finalize_no_write(publication_id, plan, skipped=len(findings))

        if plan.status is ReviewPublicationStatus.SKIPPED_NO_FINDINGS:
            logger.info("review_publish_skipped_no_findings", publication_id=str(publication_id))
            return await self._finalize_no_write(publication_id, plan)

        if mode is ReviewPublicationMode.DRY_RUN:
            logger.info(
                "review_publish_dry_run_completed",
                publication_id=str(publication_id),
                inline=len(plan.inline_comments),
                summary_only=len(plan.summary_only),
                omitted=len(plan.omitted),
            )
            return await self._finalize_no_write(publication_id, plan)

        if not config.enabled:
            logger.info("review_publish_disabled_by_config", publication_id=str(publication_id))
            return await self._finalize_no_write(
                publication_id,
                plan,
                status_override=ReviewPublicationStatus.SKIPPED_DISABLED,
                reason_override="publishing disabled by repository .patchfrog.yml publish.enabled=false",
            )

        # Final race check immediately before the real write (section 28).
        try:
            final_head_sha = await self._publisher.get_head_sha(ref)
        except Exception as exc:
            return await self._fail(publication_id, exc, skipped=len(findings))

        if final_head_sha != snapshot.head_sha:
            async with self._session_factory() as session:
                model = await self._publication_repo.mark_failed(
                    session,
                    publication_id=publication_id,
                    error_message=f"HEAD_SHA_MISMATCH at final write check: expected {snapshot.head_sha!r}, got {final_head_sha!r}",
                )
                model.status = ReviewPublicationStatus.STALE
                await session.commit()
            logger.info("review_publish_stale", publication_id=str(publication_id), reason="race_before_write")
            return self._result_from_model(model, reconciled=False, errors=(), skipped=len(findings))

        comments_input = [
            GitHubReviewCommentInput(
                path=c.position.path,
                body=c.body,
                line=c.position.line,
                side=_DIFF_SIDE_TO_GITHUB[c.position.side],
                start_line=c.position.start_line,
                start_side=(_DIFF_SIDE_TO_GITHUB[c.position.start_side] if c.position.start_side is not None else None),
            )
            for c in plan.inline_comments
            if c.position is not None
        ]

        try:
            submitted = await self._publisher.publish_review(
                ref=ref,
                commit_id=snapshot.head_sha,
                body=plan.summary_body,
                event=GitHubReviewEvent.COMMENT,
                comments=comments_input,
            )
        except Exception as exc:
            return await self._fail(publication_id, exc)

        async with self._session_factory() as session:
            model = await self._publication_repo.mark_published(
                session,
                publication_id=publication_id,
                github_review_id=submitted.id,
                inline_count=len(plan.inline_comments),
                summary_only_count=len(plan.summary_only),
                omitted_count=len(plan.omitted),
            )
            await session.commit()

        logger.info(
            "review_publish_completed",
            publication_id=str(publication_id),
            github_review_id=submitted.id,
            inline=len(plan.inline_comments),
        )
        return self._result_from_model(model, reconciled=False, errors=())

    async def _persist_plan_comments(self, publication_id: uuid.UUID, plan: ReviewPublicationPlan) -> None:
        all_comments: list[ReviewPublicationComment] = [
            *plan.inline_comments, *plan.summary_only, *plan.omitted, *plan.already_reported,
        ]
        if not all_comments:
            return
        async with self._session_factory() as session:
            for comment in all_comments:
                await self._comment_repo.create(session, review_publication_id=publication_id, comment=comment)
            await session.commit()

    async def _finalize_no_write(
        self,
        publication_id: uuid.UUID,
        plan: ReviewPublicationPlan,
        *,
        status_override: ReviewPublicationStatus | None = None,
        reason_override: str | None = None,
        skipped: int = 0,
    ) -> ReviewPublicationResult:
        async with self._session_factory() as session:
            model = await self._publication_repo.get_by_id(session, publication_id=publication_id)
            assert model is not None
            model.status = status_override or plan.status
            model.reason = reason_override or plan.reason
            model.inline_count = len(plan.inline_comments)
            model.summary_only_count = len(plan.summary_only)
            model.omitted_count = len(plan.omitted)
            model.completed_at = datetime.now(UTC)
            await session.commit()
        return self._result_from_model(model, reconciled=False, errors=(), skipped=skipped)

    async def _fail(self, publication_id: uuid.UUID, exc: Exception, *, skipped: int = 0) -> ReviewPublicationResult:
        failure_class, detail = classify_github_exception(exc)
        async with self._session_factory() as session:
            model = await self._publication_repo.mark_failed(
                session, publication_id=publication_id, error_message=f"{failure_class.value}: {detail}"
            )
            await session.commit()
        logger.error("review_publish_failed", publication_id=str(publication_id), failure_class=failure_class.value, detail=detail)
        return self._result_from_model(model, reconciled=False, errors=(detail,), skipped=skipped)

    @staticmethod
    def _result_from_model(
        model: ReviewPublicationModel, *, reconciled: bool, errors: tuple[str, ...], skipped: int = 0
    ) -> ReviewPublicationResult:
        return ReviewPublicationResult(
            status=model.status,
            mode=model.mode,
            repository_id=model.repository_id,
            pull_request_number=model.pull_request_number,
            head_sha=model.head_sha,
            publication_id=model.id,
            github_review_id=model.github_review_id,
            planned_inline=model.inline_count,
            published_inline=model.inline_count if model.status == ReviewPublicationStatus.PUBLISHED else 0,
            summary_only=model.summary_only_count,
            skipped=skipped,
            omitted=model.omitted_count,
            reconciled=reconciled,
            errors=errors,
        )
