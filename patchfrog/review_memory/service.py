"""Phase 7 orchestration: incremental review + review memory.

Two entry points, called by :mod:`apps.worker.tasks.review_pull_request`
(and, for local/dry-run use, the CLI) around Phase 5's own
:class:`patchfrog.review.service.PullRequestReviewService`, which is
never modified beyond its existing ``candidate_filter``/
``incremental_context_fingerprint`` hooks:

    prepare(...)   -- BEFORE any AI call. Finds the previous usable
                      review generation for this PR (if any), proves its
                      ancestry with real git plumbing, builds the
                      commit-to-commit change set (file + symbol
                      continuity), resolves every open memory finding's
                      pre-review disposition, and narrows Phase 5's full
                      candidate list down to what genuinely needs a fresh
                      AI look. Returns a :class:`PreparedReview` whose
                      ``candidate_filter``/``incremental_context_fingerprint``
                      feed directly into
                      :meth:`~patchfrog.review.service.PullRequestReviewService.review_pull_request`.

    finalize(...)  -- AFTER the AI review run has completed and
                      persisted. Loads this run's actual final findings,
                      reconciles them against the pre-review dispositions
                      (did a rechecked finding reappear, change, or
                      vanish?), and persists the new
                      :class:`~patchfrog.persistence.models.review_memory.ReviewGenerationModel`
                      row plus every finding-lifecycle transition. Also
                      accepts genuinely new findings into memory.
                      Idempotent: a second call for the same
                      ``review_run_id`` (crash/retry) is a no-op.

Every ambiguous or unverifiable situation collapses to "no reuse, fresh
review" -- never a guess (see :mod:`patchfrog.review_memory.domain`'s
module docstring). Ancestry verification and the git-diff fetch it
shares (:func:`patchfrog.repository.ancestry.verify_ancestor_with_diff`)
always run *before* any write-session is opened -- no DB lock, no
transaction, is ever held across that network I/O.

Known, deliberate simplification (documented rather than silently
assumed): this phase does not implement real evidence-snippet
revalidation (re-reading the current commit's file content to confirm a
finding's quoted evidence still appears verbatim at its mapped location).
:meth:`patchfrog.review_memory.resolver.ReviewMemoryResolver.resolve_pre_review`
already fails closed on this (``evidence_still_present`` omitted ==
"not confirmed"), so every UNCHANGED/MOVED/RENAMED symbol with an
attached previous finding is routed to ``NEEDS_RECHECK`` rather than a
zero-AI-call ``CARRY_FORWARD``. This is *safe* (never wrongly suppresses
a finding that might no longer apply) but gives up some of the possible
incremental savings -- the primary savings mechanism that remains fully
intact is skipping candidates for symbols that changed not at all and
never had a finding attached in the first place.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from patchfrog.analysis.queries import AnalysisQueryService
from patchfrog.diff.models import DiffFile
from patchfrog.persistence.repositories import (
    AIFindingRepository,
    IndexedFileRepository,
    ReviewCandidateRepository,
    ReviewGenerationRepository,
    ReviewMemoryFindingRepository,
    ReviewRunRepository,
    SymbolRepository,
)
from patchfrog.persistence.repositories.analysis_run import AnalysisRunRepository
from patchfrog.repository.ancestry import verify_ancestor_with_diff
from patchfrog.review.candidates import ReviewCandidateGenerator
from patchfrog.review.config import (
    REVIEW_ENGINE_VERSION,
    REVIEW_POLICY_VERSION,
    REVIEW_PROMPT_VERSION,
)
from patchfrog.review.domain import ReviewCandidate, ReviewRunStatus
from patchfrog.review_memory.config import (
    NO_MEMORY_CONTEXT_FINGERPRINT,
    IncrementalConfig,
    compute_incremental_context_fingerprint,
    compute_memory_compatibility_fingerprint,
)
from patchfrog.review_memory.domain import (
    CurrentFinding,
    FindingMemoryStatus,
    FindingReconciliation,
    IncrementalChangeSet,
    IncrementalModePolicy,
    IncrementalPlan,
    PreviousGenerationCandidate,
    PreviousReviewSelection,
    SymbolSnapshot,
    TransitionReasonCode,
)
from patchfrog.review_memory.fingerprint import (
    compute_exact_fingerprint,
    compute_semantic_family_fingerprint,
)
from patchfrog.review_memory.planner import IncrementalReviewPlanner
from patchfrog.review_memory.resolver import ReviewMemoryResolver
from patchfrog.review_memory.symbol_continuity import match_symbols

logger = structlog.get_logger(__name__)


@dataclass(slots=True)
class PreparedReview:
    """Everything :mod:`patchfrog.review.service` and
    :meth:`IncrementalReviewMemoryService.finalize` need for one review
    run -- the impure (contains a plain Python callable) counterpart to
    the pure :class:`~patchfrog.review_memory.domain.IncrementalPlan` it
    wraps."""

    plan: IncrementalPlan
    candidate_filter: Callable[[ReviewCandidate], bool]
    incremental_context_fingerprint: str
    previous_generation_id: uuid.UUID | None
    previous_commit_sha: str | None
    change_set: IncrementalChangeSet | None
    memory_compatibility_fingerprint: str
    current_symbol_snapshots: dict[uuid.UUID, SymbolSnapshot]
    #: Whether :meth:`IncrementalReviewMemoryService.finalize` should be
    #: called at all after the review run completes -- ``False`` only
    #: when Phase 7 is fully out of the picture for this run (no PR,
    #: ``memory.enabled: false``, or ``review.incremental: off``), so no
    #: generation row is ever created and this PR's memory (if any from
    #: an earlier configuration) is left untouched.
    memory_tracking_active: bool


def _selected_candidate_filter(selected: tuple[ReviewCandidate, ...]) -> Callable[[ReviewCandidate], bool]:
    selected_set = frozenset(selected)
    return lambda c: c in selected_set


class IncrementalReviewMemoryService:
    def __init__(self, *, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory
        self._generation_repo = ReviewGenerationRepository()
        self._memory_finding_repo = ReviewMemoryFindingRepository()
        self._run_repo = ReviewRunRepository()
        self._candidate_repo = ReviewCandidateRepository()
        self._finding_repo = AIFindingRepository()
        self._symbol_repo = SymbolRepository()
        self._indexed_file_repo = IndexedFileRepository()
        self._analysis_runs = AnalysisRunRepository()
        self._candidates = ReviewCandidateGenerator()
        self._resolver = ReviewMemoryResolver()
        self._planner = IncrementalReviewPlanner()

    async def build_candidates(
        self,
        *,
        repository_id: uuid.UUID,
        repository_index_id: uuid.UUID,
        commit_sha: str,
        diff_files: list[DiffFile],
        max_candidates: int,
    ) -> tuple[ReviewCandidate, ...]:
        """Phase 5's own full, deterministic candidate list -- computed
        here (redundantly with what :meth:`PullRequestReviewService._execute_and_persist`
        will compute again internally) purely for planning/metrics
        purposes. Never a different candidate set, and never an extra
        LLM call -- see the module docstring of :mod:`patchfrog.review_memory`."""

        async with self._session_factory() as session:
            static_findings = []
            latest_analysis = await self._analysis_runs.get_latest_succeeded_for_commit(
                session, repository_id=repository_id, commit_sha=commit_sha
            )
            if latest_analysis is not None:
                static_findings = await AnalysisQueryService().get_findings_for_run(
                    session, analysis_run_id=latest_analysis.id
                )
            return await self._candidates.generate(
                session,
                repository_index_id=repository_index_id,
                diff_files=diff_files,
                static_findings=static_findings,
                max_candidates=max_candidates,
            )

    async def prepare(
        self,
        *,
        pull_request_id: uuid.UUID | None,
        repository_index_id: uuid.UUID,
        commit_sha: str,
        clone_url: str,
        token: str | None,
        current_candidates: Sequence[ReviewCandidate],
        reviewer_provider: str,
        reviewer_model: str,
        incremental_config: IncrementalConfig,
    ) -> PreparedReview:
        current_candidates = tuple(current_candidates)

        if (
            pull_request_id is None
            or not incremental_config.memory_enabled
            or incremental_config.incremental is IncrementalModePolicy.OFF
        ):
            plan = self._planner.plan(
                selection=_disabled_selection(),
                current_candidates=current_candidates,
                change_set=None,
                pre_review_decisions=(),
                previous_candidate_count=0,
            )
            return PreparedReview(
                plan=plan,
                candidate_filter=lambda c: True,
                incremental_context_fingerprint=NO_MEMORY_CONTEXT_FINGERPRINT,
                previous_generation_id=None,
                previous_commit_sha=None,
                change_set=None,
                memory_compatibility_fingerprint="",
                current_symbol_snapshots={},
                memory_tracking_active=False,
            )

        current_compat_fp = compute_memory_compatibility_fingerprint(
            review_engine_version=REVIEW_ENGINE_VERSION,
            review_prompt_version=REVIEW_PROMPT_VERSION,
            review_policy_version=REVIEW_POLICY_VERSION,
            reviewer_provider=reviewer_provider,
            reviewer_model=reviewer_model,
            toolchain_fingerprint=None,
        )

        async with self._session_factory() as session:
            current_symbol_snapshots = await self._load_symbol_snapshots(
                session, repository_index_id=repository_index_id
            )
            prev = await self._generation_repo.get_latest_for_pr(session, pull_request_id=pull_request_id)

        if prev is None:
            return self._no_memory_prepared(
                current_candidates=current_candidates,
                reason=TransitionReasonCode.NO_PREVIOUS_REVIEW,
                detail="no previous review generation exists for this pull request",
                current_compat_fp=current_compat_fp,
                current_symbol_snapshots=current_symbol_snapshots,
            )

        async with self._session_factory() as session:
            prev_run = await self._run_repo.get_by_id(session, run_id=prev.review_run_id)

        if prev_run is None or prev_run.status is not ReviewRunStatus.SUCCEEDED:
            status_label = prev_run.status.value if prev_run is not None else "missing"
            return self._no_memory_prepared(
                current_candidates=current_candidates,
                reason=TransitionReasonCode.PARTIAL_PREVIOUS_REVIEW,
                detail=f"previous review run status was {status_label!r}, not succeeded",
                current_compat_fp=current_compat_fp,
                current_symbol_snapshots=current_symbol_snapshots,
                previous_generation_id=prev.id,
                previous_commit_sha=prev.commit_sha,
            )

        # No DB session open across this network call -- ancestry proof
        # (a full-history fetch) never happens while holding a lock.
        ancestry_result, file_change_set = verify_ancestor_with_diff(
            clone_url=clone_url, ancestor_sha=prev.commit_sha, descendant_sha=commit_sha, token=token
        )
        if not (ancestry_result.verified and ancestry_result.is_ancestor):
            reason = (
                TransitionReasonCode.HISTORY_REWRITTEN
                if ancestry_result.verified
                else TransitionReasonCode.ANCESTRY_UNVERIFIABLE
            )
            return self._no_memory_prepared(
                current_candidates=current_candidates,
                reason=reason,
                detail=ancestry_result.detail,
                current_compat_fp=current_compat_fp,
                current_symbol_snapshots=current_symbol_snapshots,
                previous_generation_id=prev.id,
                previous_commit_sha=prev.commit_sha,
            )
        assert file_change_set is not None  # proven ancestor => diff always computed

        compatibility_ok = current_compat_fp == prev.memory_compatibility_fingerprint

        async with self._session_factory() as session:
            previous_symbol_snapshots = await self._load_symbol_snapshots(
                session, repository_index_id=prev_run.repository_index_id
            )
            previous_findings = await self._memory_finding_repo.get_open_for_pr(
                session, pull_request_id=pull_request_id
            )
            previous_candidates = await self._candidate_repo.list_for_run(
                session, review_run_id=prev.review_run_id
            )

        symbol_changes = match_symbols(
            previous_symbols=list(previous_symbol_snapshots.values()),
            current_symbols=list(current_symbol_snapshots.values()),
            file_changes=file_change_set,
        )
        change_set = IncrementalChangeSet(
            previous_commit_sha=prev.commit_sha,
            current_commit_sha=commit_sha,
            file_changes=file_change_set,
            symbol_changes=symbol_changes,
        )

        pre_review_decisions = self._resolver.resolve_pre_review(
            previous_findings=previous_findings, change_set=change_set
        )

        selection = PreviousReviewSelection(
            candidate=PreviousGenerationCandidate(
                generation_id=prev.id,
                review_run_id=prev.review_run_id,
                commit_sha=prev.commit_sha,
                repository_index_id=prev_run.repository_index_id,
                memory_compatibility_fingerprint=prev.memory_compatibility_fingerprint,
            ),
            ancestry_verified=True,
            compatibility_ok=compatibility_ok,
            usable_for_candidate_skipping=compatibility_ok,
            usable_for_finding_memory=True,
            invalidation_reason=None if compatibility_ok else TransitionReasonCode.MODEL_DRIFT,
            detail=(
                "ancestry verified; toolchain compatible"
                if compatibility_ok
                else "ancestry verified; reviewer toolchain changed since the previous review -- "
                "candidate skipping disabled, finding memory still retained"
            ),
        )

        plan = self._planner.plan(
            selection=selection,
            current_candidates=current_candidates,
            change_set=change_set,
            pre_review_decisions=pre_review_decisions,
            previous_candidate_count=len(previous_candidates),
        )

        logger.info(
            "review_memory_prepared",
            pull_request_id=str(pull_request_id),
            mode=plan.mode.value,
            previous_generation_id=str(prev.id),
            candidates_skipped=plan.metrics.candidates_skipped_by_memory,
            candidates_selected=len(plan.selected_candidates),
        )

        return PreparedReview(
            plan=plan,
            candidate_filter=_selected_candidate_filter(plan.selected_candidates),
            incremental_context_fingerprint=compute_incremental_context_fingerprint(
                mode=plan.mode.value, previous_generation_id=str(prev.id)
            ),
            previous_generation_id=prev.id,
            previous_commit_sha=prev.commit_sha,
            change_set=change_set,
            memory_compatibility_fingerprint=current_compat_fp,
            current_symbol_snapshots=current_symbol_snapshots,
            memory_tracking_active=True,
        )

    def _no_memory_prepared(
        self,
        *,
        current_candidates: tuple[ReviewCandidate, ...],
        reason: TransitionReasonCode,
        detail: str,
        current_compat_fp: str,
        current_symbol_snapshots: dict[uuid.UUID, SymbolSnapshot],
        previous_generation_id: uuid.UUID | None = None,
        previous_commit_sha: str | None = None,
    ) -> PreparedReview:
        selection = PreviousReviewSelection(
            candidate=None,
            ancestry_verified=False,
            compatibility_ok=False,
            usable_for_candidate_skipping=False,
            usable_for_finding_memory=False,
            invalidation_reason=reason,
            detail=detail,
        )
        plan = self._planner.plan(
            selection=selection,
            current_candidates=current_candidates,
            change_set=None,
            pre_review_decisions=(),
            previous_candidate_count=0,
        )
        return PreparedReview(
            plan=plan,
            candidate_filter=lambda c: True,
            incremental_context_fingerprint=NO_MEMORY_CONTEXT_FINGERPRINT,
            previous_generation_id=previous_generation_id,
            previous_commit_sha=previous_commit_sha,
            change_set=None,
            memory_compatibility_fingerprint=current_compat_fp,
            current_symbol_snapshots=current_symbol_snapshots,
            memory_tracking_active=True,
        )

    async def finalize(
        self,
        *,
        review_run_id: uuid.UUID,
        repository_id: uuid.UUID,
        pull_request_id: uuid.UUID,
        commit_sha: str,
        prepared: PreparedReview,
    ) -> FindingReconciliation:
        """Call once, after :meth:`PullRequestReviewService.review_pull_request`
        (or ``review_local``) has returned for this exact ``review_run_id``
        -- never before its candidates/findings are persisted. Idempotent:
        if this ``review_run_id`` was already finalized (a retried Celery
        delivery, or a canonical-run reuse), this is a no-op."""

        async with self._session_factory() as session:
            existing = await self._generation_repo.get_by_review_run_id(session, review_run_id=review_run_id)
        if existing is not None:
            logger.info("review_memory_finalize_already_done", review_run_id=str(review_run_id))
            return FindingReconciliation(
                new_finding_ids=frozenset(), resolved_memory_finding_ids=frozenset(), changed_pairs=()
            )

        async with self._session_factory() as session:
            current_findings = await self._load_current_findings(
                session, review_run_id=review_run_id, symbol_snapshots=prepared.current_symbol_snapshots
            )

        decisions = self._resolver.reconcile_post_review(
            pre_review_decisions=prepared.plan.pre_review_decisions, current_findings=current_findings
        )

        matched_ids = {d.updated_current_finding_id for d in decisions if d.updated_current_finding_id is not None}
        new_findings = [cf for cf in current_findings if cf.id not in matched_ids]
        resolved_ids = frozenset(
            d.memory_finding.id for d in decisions if d.new_status is FindingMemoryStatus.RESOLVED
        )
        changed_pairs = tuple(
            (d.memory_finding.id, d.updated_current_finding_id)
            for d in decisions
            if d.new_status is FindingMemoryStatus.CHANGED and d.updated_current_finding_id is not None
        )

        async with self._session_factory() as session:
            await self._generation_repo.create(
                session,
                repository_id=repository_id,
                pull_request_id=pull_request_id,
                review_run_id=review_run_id,
                commit_sha=commit_sha,
                previous_generation_id=prepared.previous_generation_id,
                previous_commit_sha=prepared.previous_commit_sha,
                ancestry_verified=prepared.plan.selection.ancestry_verified,
                mode=prepared.plan.mode,
                compatibility_ok=prepared.plan.selection.compatibility_ok,
                invalidation_reason=prepared.plan.selection.invalidation_reason,
                memory_compatibility_fingerprint=prepared.memory_compatibility_fingerprint,
            )

            for d in decisions:
                await self._memory_finding_repo.apply_transition(
                    session,
                    memory_finding_id=d.memory_finding.id,
                    new_status=d.new_status,
                    reason=d.reason,
                    detail=d.detail,
                    target_review_run_id=review_run_id,
                    commit_sha=commit_sha,
                    updated_file_path=d.updated_file_path,
                    updated_start_line=d.updated_start_line,
                    updated_end_line=d.updated_end_line,
                    updated_symbol_id=d.updated_symbol_id,
                    updated_current_finding_id=d.updated_current_finding_id,
                )

            for cf in new_findings:
                await self._create_memory_finding_for(
                    session,
                    repository_id=repository_id,
                    pull_request_id=pull_request_id,
                    review_run_id=review_run_id,
                    commit_sha=commit_sha,
                    cf=cf,
                )

            await session.commit()

        logger.info(
            "review_memory_finalized",
            review_run_id=str(review_run_id),
            new_findings=len(new_findings),
            resolved=len(resolved_ids),
            changed=len(changed_pairs),
            carried_forward=sum(1 for d in decisions if d.new_status is FindingMemoryStatus.CARRIED_FORWARD),
        )

        return FindingReconciliation(
            new_finding_ids=frozenset(cf.id for cf in new_findings),
            resolved_memory_finding_ids=resolved_ids,
            changed_pairs=changed_pairs,
        )

    async def _create_memory_finding_for(
        self,
        session: AsyncSession,
        *,
        repository_id: uuid.UUID,
        pull_request_id: uuid.UUID,
        review_run_id: uuid.UUID,
        commit_sha: str,
        cf: CurrentFinding,
    ) -> None:
        semantic_fp = compute_semantic_family_fingerprint(
            repository_id=repository_id,
            pull_request_id=pull_request_id,
            file_path=cf.file_path,
            symbol_qualified_name=cf.symbol_qualified_name,
            category=cf.category,
            title=cf.title,
        )
        exact_fp = compute_exact_fingerprint(
            repository_id=repository_id,
            pull_request_id=pull_request_id,
            commit_sha=commit_sha,
            file_path=cf.file_path,
            start_line=cf.start_line,
            end_line=cf.end_line,
            category=cf.category,
            message=cf.message,
        )

        colliding = await self._memory_finding_repo.get_by_semantic_family(
            session, pull_request_id=pull_request_id, semantic_family_fingerprint=semantic_fp
        )
        if colliding is not None:
            # An active memory finding already occupies this semantic
            # family (e.g. an ambiguous rename produced two independently
            # "new" findings that resolve to the same family). Resolved
            # deterministically -- superseded, never silently dropped and
            # never left to crash the partial unique index.
            await self._memory_finding_repo.apply_transition(
                session,
                memory_finding_id=colliding.id,
                new_status=FindingMemoryStatus.SUPERSEDED,
                reason=TransitionReasonCode.AMBIGUOUS_SYMBOL_MATCH,
                detail="superseded by a new finding sharing the same semantic family fingerprint",
                target_review_run_id=review_run_id,
                commit_sha=commit_sha,
            )

        await self._memory_finding_repo.create(
            session,
            repository_id=repository_id,
            pull_request_id=pull_request_id,
            source_review_run_id=review_run_id,
            source_finding_id=cf.id,
            commit_sha=commit_sha,
            file_path=cf.file_path,
            symbol_id=cf.symbol_id,
            symbol_qualified_name=cf.symbol_qualified_name,
            symbol_kind=cf.symbol_kind,
            category=cf.category,
            severity=cf.severity,
            title=cf.title,
            message=cf.message,
            start_line=cf.start_line,
            end_line=cf.end_line,
            exact_fingerprint=exact_fp,
            semantic_family_fingerprint=semantic_fp,
        )

    async def _load_current_findings(
        self,
        session: AsyncSession,
        *,
        review_run_id: uuid.UUID,
        symbol_snapshots: dict[uuid.UUID, SymbolSnapshot],
    ) -> list[CurrentFinding]:
        candidates = await self._candidate_repo.list_for_run(session, review_run_id=review_run_id)
        candidate_by_id = {c.id: c for c in candidates}
        findings = await self._finding_repo.list_for_run(session, review_run_id=review_run_id)

        result: list[CurrentFinding] = []
        for f in findings:
            candidate = candidate_by_id.get(f.candidate_id)
            symbol_id = candidate.symbol_id if candidate is not None else None
            snapshot = symbol_snapshots.get(symbol_id) if symbol_id is not None else None
            result.append(
                CurrentFinding(
                    id=f.id,
                    symbol_id=symbol_id,
                    symbol_qualified_name=candidate.qualified_name if candidate is not None else None,
                    symbol_kind=snapshot.kind if snapshot is not None else None,
                    file_path=f.file_path,
                    category=f.category,
                    severity=f.severity,
                    title=f.title,
                    message=f.message,
                    start_line=f.start_line,
                    end_line=f.end_line,
                )
            )
        return result

    async def _load_symbol_snapshots(
        self, session: AsyncSession, *, repository_index_id: uuid.UUID
    ) -> dict[uuid.UUID, SymbolSnapshot]:
        symbols = await self._symbol_repo.list_for_index(session, repository_index_id=repository_index_id)
        if not symbols:
            return {}
        files = await self._indexed_file_repo.list_for_index(session, repository_index_id=repository_index_id)
        path_by_file_id = {f.id: f.relative_path for f in files}
        return {
            s.id: SymbolSnapshot(
                id=s.id,
                name=s.name,
                qualified_name=s.qualified_name,
                kind=s.kind,
                language=s.language,
                file_path=path_by_file_id.get(s.indexed_file_id, ""),
                start_line=s.start_line,
                end_line=s.end_line,
                content_hash=s.content_hash,
            )
            for s in symbols
        }


def _disabled_selection() -> PreviousReviewSelection:
    return PreviousReviewSelection(
        candidate=None,
        ancestry_verified=False,
        compatibility_ok=False,
        usable_for_candidate_skipping=False,
        usable_for_finding_memory=False,
        invalidation_reason=None,
        detail="review memory disabled (no pull request, memory.enabled=false, or review.incremental=off)",
    )
