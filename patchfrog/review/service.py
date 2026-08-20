"""AI Reviewer orchestration.

Wires together: stale-index protection -> candidate generation -> bounded,
concurrent, budget-guarded provider calls -> deterministic
evidence/location validation -> critic review -> confidence aggregation ->
AI/AI dedup -> idempotent/concurrency-safe persistence. Mirrors
:mod:`patchfrog.context.service` and :mod:`patchfrog.analysis.service`'s
transaction strategy: the ``review_runs`` row (status ``running``) is
committed immediately so it's observable while candidates are reviewed,
everything computed in memory, then one second transaction persists
candidates/proposals/verdicts/findings only after ``claim_for_write``
confirms this run still owns its identity. A worker crash between those
two transactions leaves an orphaned ``running`` row and nothing else --
retrying the same PR/commit creates a fresh run under the identical
identity (see :class:`~patchfrog.persistence.repositories.review_run.ReviewRunRepository`),
exactly the same recovery story Phase 3/4 already established.

"The model may propose. PatchFrog decides what survives." -- this module
is where that decision is enforced end to end.
"""

from __future__ import annotations

import asyncio
import hashlib
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import replace
from pathlib import Path

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from patchfrog.analysis.queries import AnalysisQueryService
from patchfrog.context.config import ContextConfig
from patchfrog.context.domain import ContextTargetType
from patchfrog.context.service import ContextService
from patchfrog.context.tokens import estimate_tokens
from patchfrog.diff.models import DiffFile, DiffHunk
from patchfrog.intelligence.queries import RepositoryQueryService
from patchfrog.persistence.models.analysis import FindingModel
from patchfrog.persistence.models.review import ReviewRunModel
from patchfrog.persistence.repositories import (
    AIFindingProposalRepository,
    AIFindingRepository,
    CriticVerdictRepository,
    ReviewCandidateRepository,
    ReviewRunRepository,
)
from patchfrog.persistence.repositories.analysis_run import AnalysisRunRepository
from patchfrog.persistence.repositories.repository_index import RepositoryIndexRepository
from patchfrog.review.candidates import ReviewCandidateGenerator, summarize_static_finding
from patchfrog.review.confidence import aggregate, meets_minimum
from patchfrog.review.config import MalformedReviewConfigError, ReviewConfig, ReviewModelIdentity
from patchfrog.review.critic import CriticService
from patchfrog.review.dedup import deduplicate
from patchfrog.review.domain import (
    CriticDecision,
    CriticVerdict,
    FinalAIFinding,
    ProposalStatus,
    ReviewCandidate,
    ReviewRunStatus,
    ReviewRunSummary,
    TokenUsage,
    ValidatedFinding,
    ValidationOutcome,
)
from patchfrog.review.prompt import build_reviewer_prompt
from patchfrog.review.provider import (
    LLMProvider,
    ProviderFatalError,
    ProviderRequest,
    ProviderTransientError,
)
from patchfrog.review.redaction import redact_secrets
from patchfrog.review.schemas import REVIEW_RESPONSE_SCHEMA
from patchfrog.review.validation import (
    ResponseSchemaError,
    ValidationContext,
    parse_and_validate_response,
)

logger = structlog.get_logger(__name__)


class StaleReviewIndexError(RuntimeError):
    """No Phase 2 repository index exists for the exact commit being
    reviewed. Mirrors :class:`patchfrog.context.service.StaleContextIndexError`
    and :class:`patchfrog.analysis.service.StaleIndexError`."""


async def require_matching_review_index(
    session_factory: async_sessionmaker[AsyncSession], *, repository_id: uuid.UUID, commit_sha: str
) -> uuid.UUID:
    """Shared by :meth:`PullRequestReviewService._require_matching_index`
    and :func:`persist_malformed_config_failure` -- the exact commit under
    review must have a matching Phase 2 repository index, whether or not
    its ``.patchfrog.yml`` could even be parsed. Module-level (rather than
    an instance method) specifically so it's callable before a
    :class:`PullRequestReviewService` exists -- config resolution, and
    therefore provider selection, must happen before the service is
    constructed (see :mod:`patchfrog.review.config_resolution`)."""

    index_repo = RepositoryIndexRepository()
    async with session_factory() as session:
        active_index = await index_repo.get_active(session, repository_id=repository_id)
    if active_index is None:
        raise StaleReviewIndexError(f"no repository index exists for repository {repository_id}")
    if active_index.commit_sha != commit_sha:
        raise StaleReviewIndexError(
            f"repository index is at commit {active_index.commit_sha!r}, "
            f"but review was requested for commit {commit_sha!r}"
        )
    return active_index.id


#: Fixed sentinel model_fingerprint for a malformed-config failure record
#: (see :func:`persist_malformed_config_failure`) -- no real reviewer
#: provider/model was ever selected (the configured model name lives in
#: the very file that failed to parse), so there is nothing meaningful to
#: fingerprint there. The failure's true identity comes entirely from its
#: content-addressed config_fingerprint (see :func:`_malformed_config_fingerprint`).
_MALFORMED_CONFIG_MODEL_FINGERPRINT = hashlib.sha256(b"malformed_review_config:unresolved_model").hexdigest()


async def persist_malformed_config_failure(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    repository_id: uuid.UUID,
    commit_sha: str,
    pull_request_id: uuid.UUID | None,
    exc: MalformedReviewConfigError,
) -> ReviewRunModel:
    """Record a malformed ``.patchfrog.yml`` as a clear, queryable failed
    ``review_runs`` row -- "fail safely" means observable via the
    database, not just a raised exception nobody happens to be watching
    for. Callable directly by the CLI and the Celery task *before* either
    has resolved a provider or constructed a :class:`PullRequestReviewService`
    -- config resolution must happen before provider selection (see
    :mod:`patchfrog.review.config_resolution`), so there is no real
    reviewer provider/model to record at this point; ``reviewer_provider``/
    ``reviewer_model`` are recorded as the literal string ``"unresolved"``
    rather than a guess.

    The identity is content-addressed on the malformed file's raw text
    (:func:`_malformed_config_fingerprint`): retrying against the exact
    same bad content reuses this identity (serialized by the same
    ``pg_advisory_xact_lock`` every other review-run identity uses), while
    fixing the file naturally produces a fresh, distinct identity on the
    next attempt. This identity never reaches ``succeeded``, so unlike a
    normal identity it does not converge to one canonical row across
    repeated attempts -- each attempt is its own historical record, the
    same behavior any other run-level exception already has (see the
    generic ``except Exception`` handling in
    :meth:`PullRequestReviewService._run`).

    Still requires a matching repository index for ``commit_sha`` (reuses
    :func:`require_matching_review_index`) -- a malformed config on a
    stale/wrong commit surfaces :class:`StaleReviewIndexError` first,
    exactly as a well-formed config would.
    """

    repository_index_id = await require_matching_review_index(
        session_factory, repository_id=repository_id, commit_sha=commit_sha
    )
    run_repo = ReviewRunRepository()
    config_fingerprint = _malformed_config_fingerprint(exc)

    async with session_factory() as session:
        run, is_new = await run_repo.get_or_create_running(
            session,
            repository_id=repository_id,
            repository_index_id=repository_index_id,
            commit_sha=commit_sha,
            config_fingerprint=config_fingerprint,
            model_fingerprint=_MALFORMED_CONFIG_MODEL_FINGERPRINT,
            reviewer_provider="unresolved",
            reviewer_model="unresolved",
            critic_provider=None,
            critic_model=None,
            pull_request_id=pull_request_id,
        )
        await session.commit()

    if not is_new:
        logger.info("review_run_malformed_config_already_recorded", run_id=str(run.id))
        return run

    async with session_factory() as session:
        failed_run = await run_repo.mark_failed(
            session, run_id=run.id, error_message=f"malformed .patchfrog.yml: {exc}"
        )
        await session.commit()
    logger.error("review_run_failed_malformed_config", run_id=str(run.id), error=str(exc))
    return failed_run


class _CandidateOutcome:
    __slots__ = (
        "candidate",
        "context_bundle_id",
        "context_text",
        "critic_usage",
        "diff_excerpt",
        "error",
        "failed",
        "final",
        "reviewer_usage",
        "skipped_budget",
        "validated",
        "verdicts",
    )

    def __init__(self, candidate: ReviewCandidate) -> None:
        self.candidate = candidate
        self.context_text = ""
        self.context_bundle_id: uuid.UUID | None = None
        self.diff_excerpt = ""
        self.validated: list[ValidatedFinding] = []
        self.verdicts: dict[int, CriticVerdict | None] = {}
        self.final: list[FinalAIFinding] = []
        self.failed = False
        self.error: str | None = None
        self.skipped_budget = False
        self.reviewer_usage = TokenUsage()
        self.critic_usage = TokenUsage()


class PullRequestReviewService:
    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        reviewer_provider: LLMProvider,
        critic_provider: LLMProvider | None = None,
        query_service: RepositoryQueryService | None = None,
        candidate_generator: ReviewCandidateGenerator | None = None,
        context_service: ContextService | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._reviewer_provider = reviewer_provider
        self._critic_provider = critic_provider
        self._queries = query_service or RepositoryQueryService()
        self._candidates = candidate_generator or ReviewCandidateGenerator(query_service=self._queries)
        self._context_service = context_service or ContextService(session_factory=session_factory)
        self._critic = CriticService(provider=critic_provider) if critic_provider is not None else None
        self._run_repo = ReviewRunRepository()
        self._candidate_repo = ReviewCandidateRepository()
        self._proposal_repo = AIFindingProposalRepository()
        self._verdict_repo = CriticVerdictRepository()
        self._finding_repo = AIFindingRepository()
        self._analysis_runs = AnalysisRunRepository()

    async def review_local(
        self,
        *,
        repository_id: uuid.UUID,
        root_path: Path,
        repository_full_name: str,
        commit_sha: str,
        diff_files: list[DiffFile],
        pull_request_id: uuid.UUID | None = None,
        config: ReviewConfig | None = None,
        candidate_filter: Callable[[ReviewCandidate], bool] | None = None,
        incremental_context_fingerprint: str | None = None,
    ) -> ReviewRunSummary:
        """Review a repository already checked out on disk (CLI / dogfood
        use). Context is built against the same local checkout via
        :meth:`ContextService.build_context_local`.

        ``config`` is expected to already be resolved by the caller (see
        :func:`patchfrog.review.config_resolution.resolve_repository_review_config`,
        used by both the CLI and the production Celery task so they can
        never diverge) -- provider selection depends on ``config.provider``/
        ``config.model``, so resolution necessarily happens before a
        provider, and therefore this service, can be constructed. Omitting
        ``config`` here is only a convenience default of
        :class:`~patchfrog.review.config.ReviewConfig`'s own defaults, not
        a substitute for real resolution.

        ``candidate_filter``/``incremental_context_fingerprint`` are the
        only two Phase 7 (:mod:`patchfrog.review_memory`) hooks into this
        otherwise-unmodified Phase 5 service: an optional predicate
        applied to Phase 5's own deterministically-generated candidates
        (never a different candidate *set* -- Phase 7 only ever narrows
        it) and the run-identity fingerprint that keeps a full-mode
        result and an incremental-mode result for the same commit from
        ever being reused for each other. Both default to "Phase 7
        doesn't exist" behavior.
        """

        return await self._run(
            repository_id=repository_id,
            repository_full_name=repository_full_name,
            commit_sha=commit_sha,
            diff_files=diff_files,
            pull_request_id=pull_request_id,
            config=config or ReviewConfig(),
            context_kwargs={"root_path": root_path},
            local=True,
            candidate_filter=candidate_filter,
            incremental_context_fingerprint=incremental_context_fingerprint,
        )

    async def review_pull_request(
        self,
        *,
        repository_id: uuid.UUID,
        clone_url: str,
        commit_sha: str,
        repository_full_name: str,
        token: str | None,
        diff_files: list[DiffFile],
        pull_request_id: uuid.UUID | None = None,
        config: ReviewConfig | None = None,
        candidate_filter: Callable[[ReviewCandidate], bool] | None = None,
        incremental_context_fingerprint: str | None = None,
    ) -> ReviewRunSummary:
        """Review a repository fetched from ``clone_url`` (production /
        Celery-task use). ``config`` is expected to already be resolved by
        the caller -- see :meth:`review_local`'s docstring, including for
        ``candidate_filter``/``incremental_context_fingerprint``."""

        return await self._run(
            repository_id=repository_id,
            repository_full_name=repository_full_name,
            commit_sha=commit_sha,
            diff_files=diff_files,
            pull_request_id=pull_request_id,
            config=config or ReviewConfig(),
            context_kwargs={"clone_url": clone_url, "token": token},
            local=False,
            candidate_filter=candidate_filter,
            incremental_context_fingerprint=incremental_context_fingerprint,
        )

    async def _require_matching_index(self, *, repository_id: uuid.UUID, commit_sha: str) -> uuid.UUID:
        return await require_matching_review_index(
            self._session_factory, repository_id=repository_id, commit_sha=commit_sha
        )

    async def _run(
        self,
        *,
        repository_id: uuid.UUID,
        repository_full_name: str,
        commit_sha: str,
        diff_files: list[DiffFile],
        pull_request_id: uuid.UUID | None,
        config: ReviewConfig,
        context_kwargs: dict[str, object],
        local: bool,
        candidate_filter: Callable[[ReviewCandidate], bool] | None = None,
        incremental_context_fingerprint: str | None = None,
    ) -> ReviewRunSummary:
        start = time.monotonic()
        log = logger.bind(repository_id=str(repository_id), commit_sha=commit_sha)

        repository_index_id = await self._require_matching_index(
            repository_id=repository_id, commit_sha=commit_sha
        )

        model_identity = ReviewModelIdentity(
            reviewer_provider=self._reviewer_provider.identity.provider,
            reviewer_model=self._reviewer_provider.identity.model,
            critic_provider=(self._critic_provider.identity.provider if self._critic_provider else None),
            critic_model=(self._critic_provider.identity.model if self._critic_provider else None),
        )
        model_fingerprint = model_identity.fingerprint()
        config_fingerprint = config.fingerprint()

        async with self._session_factory() as session:
            run, is_new = await self._run_repo.get_or_create_running(
                session,
                repository_id=repository_id,
                repository_index_id=repository_index_id,
                commit_sha=commit_sha,
                config_fingerprint=config_fingerprint,
                model_fingerprint=model_fingerprint,
                reviewer_provider=model_identity.reviewer_provider,
                reviewer_model=model_identity.reviewer_model,
                critic_provider=model_identity.critic_provider,
                critic_model=model_identity.critic_model,
                pull_request_id=pull_request_id,
                incremental_context_fingerprint=incremental_context_fingerprint,
            )
            await session.commit()
            run_id = run.id

        if not is_new:
            log.info("review_run_reused", run_id=str(run_id))
            return _summary_from_model(run, reused=True)

        try:
            summary = await self._execute_and_persist(
                run_id=run_id,
                repository_id=repository_id,
                repository_index_id=repository_index_id,
                repository_full_name=repository_full_name,
                commit_sha=commit_sha,
                diff_files=diff_files,
                config=config,
                config_fingerprint=config_fingerprint,
                model_fingerprint=model_fingerprint,
                context_kwargs=context_kwargs,
                local=local,
                start=start,
                log=log,
                candidate_filter=candidate_filter,
                incremental_context_fingerprint=run.incremental_context_fingerprint,
            )
        except Exception as exc:
            async with self._session_factory() as session:
                await self._run_repo.mark_failed(session, run_id=run_id, error_message=str(exc))
                await session.commit()
            log.error("review_run_failed", run_id=str(run_id), error=str(exc))
            raise

        return summary

    async def _execute_and_persist(
        self,
        *,
        run_id: uuid.UUID,
        repository_id: uuid.UUID,
        repository_index_id: uuid.UUID,
        repository_full_name: str,
        commit_sha: str,
        diff_files: list[DiffFile],
        config: ReviewConfig,
        config_fingerprint: str,
        model_fingerprint: str,
        context_kwargs: dict[str, object],
        local: bool,
        start: float,
        log: structlog.stdlib.BoundLogger,
        candidate_filter: Callable[[ReviewCandidate], bool] | None = None,
        incremental_context_fingerprint: str,
    ) -> ReviewRunSummary:
        async with self._session_factory() as session:
            static_findings = []
            latest_analysis = await self._analysis_runs.get_latest_succeeded_for_commit(
                session, repository_id=repository_id, commit_sha=commit_sha
            )
            if latest_analysis is not None:
                analysis_queries = AnalysisQueryService()
                static_findings = await analysis_queries.get_findings_for_run(
                    session, analysis_run_id=latest_analysis.id
                )

            candidates = await self._candidates.generate(
                session,
                repository_index_id=repository_index_id,
                diff_files=diff_files,
                static_findings=static_findings,
                max_candidates=config.max_candidates,
            )

        if candidate_filter is not None:
            # Phase 7 (patchfrog.review_memory) narrowing candidates down
            # to only those genuinely changed since the previous review
            # generation -- never a different candidate *set*, only a
            # subset of exactly what Phase 5 would already review.
            candidates = tuple(c for c in candidates if candidate_filter(c))

        outcomes = [_CandidateOutcome(c) for c in candidates]
        static_by_id = {f.id: f for f in static_findings}

        budget_lock = asyncio.Lock()
        budget_state = {"used_input_tokens": 0}
        semaphore = asyncio.Semaphore(max(1, config.max_concurrent_requests))

        async def _process(outcome: _CandidateOutcome) -> None:
            async with semaphore:
                await self._review_candidate(
                    outcome,
                    repository_id=repository_id,
                    repository_full_name=repository_full_name,
                    commit_sha=commit_sha,
                    diff_files=diff_files,
                    config=config,
                    static_by_id=static_by_id,
                    context_kwargs=context_kwargs,
                    local=local,
                    budget_lock=budget_lock,
                    budget_state=budget_state,
                    log=log,
                )

        await asyncio.gather(*(_process(o) for o in outcomes))

        all_final: list[FinalAIFinding] = [f for o in outcomes for f in o.final]
        dedup_result = deduplicate(tuple(all_final))
        kept_object_ids = {id(f) for f in dedup_result.kept}

        candidates_reviewed = sum(1 for o in outcomes if not o.failed and not o.skipped_budget)
        candidates_failed = sum(1 for o in outcomes if o.failed)
        candidates_skipped_budget = sum(1 for o in outcomes if o.skipped_budget)
        proposals_count = sum(len(o.validated) for o in outcomes)
        rejected_count = proposals_count - len(all_final)
        accepted_count = len(dedup_result.kept)
        suppressed_duplicate_count = len(dedup_result.suppressed)

        reviewer_usage = TokenUsage()
        critic_usage = TokenUsage()
        for o in outcomes:
            reviewer_usage = reviewer_usage + o.reviewer_usage
            critic_usage = critic_usage + o.critic_usage

        duration_ms = (time.monotonic() - start) * 1000

        async with self._session_factory() as session:
            existing = await self._run_repo.claim_for_write(
                session,
                run_id=run_id,
                repository_id=repository_id,
                commit_sha=commit_sha,
                config_fingerprint=config_fingerprint,
                model_fingerprint=model_fingerprint,
                incremental_context_fingerprint=incremental_context_fingerprint,
            )
            if existing is not None:
                await self._run_repo.mark_failed(
                    session, run_id=run_id, error_message=f"superseded by concurrent review run {existing.id}"
                )
                await session.commit()
                log.info("review_run_superseded", run_id=str(run_id), winning_run_id=str(existing.id))
                return _summary_from_model(existing, reused=True)

            persisted_final: list[FinalAIFinding] = []
            for outcome in outcomes:
                candidate_model = await self._candidate_repo.create(
                    session, review_run_id=run_id, candidate=outcome.candidate
                )
                if outcome.skipped_budget:
                    await self._candidate_repo.mark_skipped_budget(session, candidate_id=candidate_model.id)
                    continue
                if outcome.failed:
                    await self._candidate_repo.mark_failed(
                        session, candidate_id=candidate_model.id, error_message=outcome.error or "unknown error"
                    )
                    continue
                await self._candidate_repo.mark_reviewed(
                    session, candidate_id=candidate_model.id, context_bundle_id=outcome.context_bundle_id
                )

                for idx, validated in enumerate(outcome.validated):
                    if validated.outcome != ValidationOutcome.VALID:
                        await self._proposal_repo.create(
                            session,
                            review_run_id=run_id,
                            candidate_id=candidate_model.id,
                            finding=validated.finding,
                            status=ProposalStatus.REJECTED_VALIDATION,
                            validation_detail=validated.detail,
                        )
                        continue

                    verdict = outcome.verdicts.get(idx)
                    final = next(
                        (f for f in outcome.final if f.finding is validated.finding), None
                    )

                    if verdict is not None and verdict.decision == CriticDecision.REJECT:
                        proposal = await self._proposal_repo.create(
                            session,
                            review_run_id=run_id,
                            candidate_id=candidate_model.id,
                            finding=validated.finding,
                            status=ProposalStatus.REJECTED_CRITIC,
                            validation_detail=verdict.reasoning_summary,
                        )
                        await self._verdict_repo.create(session, proposal_id=proposal.id, verdict=verdict)
                        continue

                    if final is None:
                        # Validated but did not reach minimum confidence.
                        proposal = await self._proposal_repo.create(
                            session,
                            review_run_id=run_id,
                            candidate_id=candidate_model.id,
                            finding=validated.finding,
                            status=ProposalStatus.REJECTED_LOW_CONFIDENCE,
                            validation_detail="below configured minimum final confidence",
                        )
                        if verdict is not None:
                            await self._verdict_repo.create(session, proposal_id=proposal.id, verdict=verdict)
                        continue

                    is_kept = id(final) in kept_object_ids
                    status = ProposalStatus.ACCEPTED if is_kept else ProposalStatus.SUPPRESSED_DUPLICATE
                    proposal = await self._proposal_repo.create(
                        session,
                        review_run_id=run_id,
                        candidate_id=candidate_model.id,
                        finding=validated.finding,
                        status=status,
                        validation_detail=None,
                    )
                    if verdict is not None:
                        await self._verdict_repo.create(session, proposal_id=proposal.id, verdict=verdict)

                    if is_kept:
                        persisted_final.append(
                            replace(final, proposal_id=proposal.id, candidate_id=candidate_model.id)
                        )

            if persisted_final:
                await self._finding_repo.bulk_create(session, review_run_id=run_id, findings=persisted_final)

            if candidates_reviewed == 0 and candidates_failed > 0:
                run_status = ReviewRunStatus.FAILED
            elif candidates_failed > 0:
                run_status = ReviewRunStatus.PARTIAL
            else:
                run_status = ReviewRunStatus.SUCCEEDED

            final_run = await self._run_repo.mark_succeeded(
                session,
                run_id=run_id,
                status=run_status,
                candidate_count=len(outcomes),
                candidates_reviewed=candidates_reviewed,
                candidates_failed=candidates_failed,
                candidates_skipped_budget=candidates_skipped_budget,
                proposals_count=proposals_count,
                accepted_count=accepted_count,
                rejected_count=rejected_count,
                suppressed_duplicate_count=suppressed_duplicate_count,
                reviewer_input_tokens=reviewer_usage.input_tokens,
                reviewer_output_tokens=reviewer_usage.output_tokens,
                critic_input_tokens=critic_usage.input_tokens,
                critic_output_tokens=critic_usage.output_tokens,
                duration_ms=duration_ms,
            )
            await session.commit()

        log.info(
            "review_run_completed",
            run_id=str(run_id),
            status=run_status.value,
            candidate_count=len(outcomes),
            accepted_count=accepted_count,
            rejected_count=rejected_count,
            suppressed_duplicate_count=suppressed_duplicate_count,
            duration_ms=duration_ms,
        )

        return ReviewRunSummary(
            run_id=final_run.id,
            status=run_status,
            candidate_count=len(outcomes),
            candidates_reviewed=candidates_reviewed,
            candidates_failed=candidates_failed,
            candidates_skipped_budget=candidates_skipped_budget,
            proposals_count=proposals_count,
            accepted_count=accepted_count,
            rejected_count=rejected_count,
            suppressed_duplicate_count=suppressed_duplicate_count,
            reviewer_usage=reviewer_usage,
            critic_usage=critic_usage,
            duration_ms=duration_ms,
            reused_existing_run=(final_run.id != run_id),
        )

    async def _review_candidate(
        self,
        outcome: _CandidateOutcome,
        *,
        repository_id: uuid.UUID,
        repository_full_name: str,
        commit_sha: str,
        diff_files: list[DiffFile],
        config: ReviewConfig,
        static_by_id: dict[uuid.UUID, FindingModel],
        context_kwargs: dict[str, object],
        local: bool,
        budget_lock: asyncio.Lock,
        budget_state: dict[str, int],
        log: structlog.stdlib.BoundLogger,
    ) -> None:
        candidate = outcome.candidate
        try:
            context_config = ContextConfig(
                max_tokens=max(500, int(config.max_input_tokens_per_candidate * 0.6)),
            )
            target_type = ContextTargetType.SYMBOL if candidate.symbol_id else ContextTargetType.LINE
            build_kwargs: dict[str, object] = {
                "repository_id": repository_id,
                "repository_full_name": repository_full_name,
                "target_type": target_type,
                "file_path": candidate.file_path,
                "line": candidate.start_line,
                "symbol_id": candidate.symbol_id,
                "diff_files": diff_files,
                "config": context_config,
            }
            build_kwargs.update(context_kwargs)
            if local:
                bundle = await self._context_service.build_context_local(**build_kwargs)  # type: ignore[arg-type]
            else:
                build_kwargs["commit_sha"] = commit_sha
                bundle = await self._context_service.build_context(**build_kwargs)  # type: ignore[arg-type]

            context_text = "\n\n".join(f"# {item.file_path}\n{item.content}" for item in bundle.items)
            allowed_file_paths = frozenset({candidate.file_path} | {i.file_path for i in bundle.items})
            outcome.context_bundle_id = bundle.id
        except Exception as exc:
            outcome.failed = True
            outcome.error = f"context build failed: {exc}"
            return

        diff_excerpt = _render_diff_excerpt(diff_files, candidate)
        context_redaction = redact_secrets(context_text)
        diff_redaction = redact_secrets(diff_excerpt)
        if context_redaction.redacted_count or diff_redaction.redacted_count:
            log.warning(
                "review_secrets_redacted",
                candidate_file=candidate.file_path,
                count=context_redaction.redacted_count + diff_redaction.redacted_count,
            )
        context_text = context_redaction.text
        diff_excerpt = diff_redaction.text
        outcome.context_text = context_text
        outcome.diff_excerpt = diff_excerpt

        static_summaries = tuple(
            summarize_static_finding(static_by_id[fid])
            for fid in candidate.static_finding_ids
            if fid in static_by_id
        )

        system_prompt, user_prompt = build_reviewer_prompt(
            candidate=candidate,
            context_text=context_text,
            diff_excerpt=diff_excerpt,
            static_findings=static_summaries,
        )
        estimated_input = estimate_tokens(system_prompt) + estimate_tokens(user_prompt)

        async with budget_lock:
            if budget_state["used_input_tokens"] + estimated_input > config.max_total_input_tokens:
                outcome.skipped_budget = True
                return
            budget_state["used_input_tokens"] += estimated_input

        request = ProviderRequest(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            json_schema=REVIEW_RESPONSE_SCHEMA,
            schema_name="review_response",
            max_output_tokens=config.max_output_tokens_per_candidate,
        )

        try:
            result = await _call_with_retry(
                lambda: self._reviewer_provider.generate_structured(request), max_retries=config.max_retries
            )
        except (ProviderFatalError, ProviderTransientError) as exc:
            outcome.failed = True
            outcome.error = f"reviewer call failed: {exc}"
            return

        outcome.reviewer_usage = TokenUsage(
            input_tokens=result.usage.input_tokens, output_tokens=result.usage.output_tokens
        )

        validation_context = ValidationContext(
            allowed_file_paths=allowed_file_paths, context_text=context_text, diff_excerpt=diff_excerpt
        )
        try:
            validated = parse_and_validate_response(result.raw_json, context=validation_context)
        except ResponseSchemaError as exc:
            outcome.failed = True
            outcome.error = f"reviewer response schema error: {exc}"
            return

        outcome.validated = validated

        for idx, v in enumerate(validated):
            if v.outcome != ValidationOutcome.VALID:
                continue

            verdict: CriticVerdict | None = None
            if self._critic is not None and config.critic_enabled:
                narrowed_critic: CriticService = self._critic
                try:

                    async def _critique(
                        v: ValidatedFinding = v, critic: CriticService = narrowed_critic
                    ) -> CriticVerdict:
                        return await critic.critique(v, candidate=candidate, context_text=context_text)

                    verdict = await _call_with_retry(_critique, max_retries=config.max_retries)
                except (ProviderFatalError, ProviderTransientError, ResponseSchemaError) as exc:
                    # A critic failure never crashes the run or discards a
                    # proposal -- it falls back to no-critic aggregation,
                    # since the deterministic validation gate already ran.
                    log.warning("review_critic_failed", candidate_file=candidate.file_path, error=str(exc))
                    verdict = None

            outcome.verdicts[idx] = verdict
            if verdict is not None:
                outcome.critic_usage = outcome.critic_usage + TokenUsage(
                    input_tokens=verdict.input_tokens, output_tokens=verdict.output_tokens
                )

            if verdict is not None and verdict.decision == CriticDecision.REJECT:
                continue

            corroborated = bool(candidate.static_finding_ids)
            aggregated = aggregate(
                reviewer_confidence=v.finding.confidence,
                reviewer_severity=v.finding.severity,
                critic_verdict=verdict,
                corroborated_by_static=corroborated,
            )
            if not meets_minimum(aggregated.final_confidence, minimum=config.min_final_confidence):
                continue

            outcome.final.append(
                FinalAIFinding(
                    proposal_id=uuid.uuid4(),  # placeholder, overwritten once the proposal row is persisted
                    candidate_id=uuid.uuid4(),
                    candidate=candidate,
                    finding=v.finding,
                    critic_verdict=verdict,
                    final_severity=aggregated.final_severity,
                    final_confidence=aggregated.final_confidence,
                    corroborated_by_static=aggregated.corroborated_by_static,
                    static_finding_ids=candidate.static_finding_ids,
                )
            )


def _malformed_config_fingerprint(exc: MalformedReviewConfigError) -> str:
    """Content-addressed identity for a malformed-config run-failure
    record -- see :meth:`PullRequestReviewService._persist_malformed_config_failure`.
    Deliberately keyed on the raw file text (not just the error message),
    so two different malformed contents that happen to produce the same
    YAML-parser error string still get distinct identities."""

    payload = f"malformed_review_config:{exc.path}:{exc.raw_text}"
    return hashlib.sha256(payload.encode()).hexdigest()


async def _call_with_retry[T](
    coro_factory: Callable[[], Awaitable[T]], *, max_retries: int, base_delay: float = 0.5
) -> T:
    """Bounded retry, transient failures only. ``ProviderFatalError`` and
    :class:`~patchfrog.review.validation.ResponseSchemaError` propagate
    immediately -- retrying a schema-invalid or auth/400 response would
    just reproduce the identical failure."""

    attempt = 0
    while True:
        try:
            return await coro_factory()
        except ProviderTransientError:
            if attempt >= max_retries:
                raise
            await asyncio.sleep(base_delay * (2**attempt))
            attempt += 1


def _render_diff_excerpt(diff_files: list[DiffFile], candidate: ReviewCandidate) -> str:
    for diff_file in diff_files:
        if diff_file.path != candidate.file_path:
            continue
        lines: list[str] = []
        for hunk in diff_file.hunks:
            hunk_lines = _hunk_lines_in_range(hunk, candidate.start_line, candidate.end_line)
            lines.extend(hunk_lines)
        if lines:
            return "\n".join(lines)
    return ""


def _hunk_lines_in_range(hunk: DiffHunk, start_line: int, end_line: int) -> list[str]:
    result = []
    for line in hunk.lines:
        relevant_line = line.new_line_number if line.new_line_number is not None else line.old_line_number
        if relevant_line is None or not (start_line <= relevant_line <= end_line):
            continue
        prefix = {"addition": "+", "deletion": "-", "context": " "}[line.line_type.value]
        result.append(f"{prefix}{relevant_line}: {line.content}")
    return result


def _summary_from_model(run: ReviewRunModel, *, reused: bool) -> ReviewRunSummary:
    return ReviewRunSummary(
        run_id=run.id,
        status=run.status,
        candidate_count=run.candidate_count,
        candidates_reviewed=run.candidates_reviewed,
        candidates_failed=run.candidates_failed,
        candidates_skipped_budget=run.candidates_skipped_budget,
        proposals_count=run.proposals_count,
        accepted_count=run.accepted_count,
        rejected_count=run.rejected_count,
        suppressed_duplicate_count=run.suppressed_duplicate_count,
        reviewer_usage=TokenUsage(
            input_tokens=run.reviewer_input_tokens, output_tokens=run.reviewer_output_tokens
        ),
        critic_usage=TokenUsage(input_tokens=run.critic_input_tokens, output_tokens=run.critic_output_tokens),
        duration_ms=run.duration_ms or 0.0,
        reused_existing_run=reused,
    )
