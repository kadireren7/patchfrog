"""Context Engine orchestration.

Wires together: stale-index protection -> snapshot acquisition -> target
resolution -> candidate generation -> scoring -> deduplication ->
budgeting -> idempotent/concurrency-safe persistence, as one context
bundle tied to a commit SHA and a target. Mirrors
:mod:`patchfrog.analysis.service`'s transaction strategy exactly: the
``context_bundles`` row (status ``running``) is committed immediately so
it's observable while candidates are generated/scored, then every item
row is inserted in one second transaction that only commits once
everything has succeeded. Any exception before that commit leaves nothing
persisted beyond the ``running`` row, which a third, independent
transaction marks ``failed``.
"""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from pathlib import Path

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from patchfrog.analysis.changed_lines import build_changed_lines_by_file
from patchfrog.analysis.domain import FindingCategory
from patchfrog.context.budgeting import ContextBudgeter
from patchfrog.context.candidates import ContextCandidateGenerator
from patchfrog.context.config import CONTEXT_ENGINE_VERSION, ContextConfig
from patchfrog.context.dedup import ScoredCandidate, deduplicate
from patchfrog.context.domain import (
    AdaptiveContextMetrics,
    ContextBundle,
    ContextItem,
    ContextQualityMetrics,
    ContextTarget,
    ContextTargetType,
    ExpansionDecision,
    ExpansionReason,
    ScoreComponent,
)
from patchfrog.context.scoring import score_candidate
from patchfrog.context.snippets import ContextSnippetService
from patchfrog.diff.models import DiffFile
from patchfrog.intelligence.queries import RepositoryQueryService
from patchfrog.persistence.models.analysis import FindingModel
from patchfrog.persistence.models.context import ContextBundleModel, ContextItemModel
from patchfrog.persistence.repositories import (
    ContextBundleRepository,
    ContextItemRepository,
    RepositoryIndexRepository,
)
from patchfrog.repository.snapshot import RepositorySnapshot, RepositorySnapshotProvider

logger = structlog.get_logger(__name__)


class StaleContextIndexError(RuntimeError):
    """No Phase 2 repository index exists for the exact commit being
    used to build context.

    Mirrors :class:`patchfrog.analysis.service.StaleIndexError` -- context
    is only ever built against repository intelligence for the *same*
    commit, never a stale or differently-versioned index.
    """


class ContextService:
    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        snapshot_provider: RepositorySnapshotProvider | None = None,
        query_service: RepositoryQueryService | None = None,
        candidate_generator: ContextCandidateGenerator | None = None,
        budgeter: ContextBudgeter | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._snapshot_provider = snapshot_provider or RepositorySnapshotProvider()
        self._queries = query_service or RepositoryQueryService()
        self._candidates = candidate_generator or ContextCandidateGenerator(query_service=self._queries)
        self._budgeter = budgeter or ContextBudgeter(snippet_service=ContextSnippetService())
        self._bundle_repo = ContextBundleRepository()
        self._item_repo = ContextItemRepository()

    async def build_context_local(
        self,
        *,
        repository_id: uuid.UUID,
        root_path: Path,
        repository_full_name: str,
        target_type: ContextTargetType,
        file_path: str,
        line: int | None = None,
        symbol_id: uuid.UUID | None = None,
        finding_id: uuid.UUID | None = None,
        analysis_run_id: uuid.UUID | None = None,
        diff_files: list[DiffFile] | None = None,
        config: ContextConfig | None = None,
    ) -> ContextBundle:
        """Build context for a repository already checked out on disk
        (developer/CLI use)."""

        snapshot = self._snapshot_provider.acquire_local(
            root_path=root_path, repository_full_name=repository_full_name
        )
        try:
            repository_index_id = await self._require_matching_index(
                repository_id=repository_id, commit_sha=snapshot.commit_sha
            )
            return await self._run(
                snapshot=snapshot,
                repository_id=repository_id,
                repository_index_id=repository_index_id,
                commit_sha=snapshot.commit_sha,
                target_type=target_type,
                file_path=file_path,
                line=line,
                symbol_id=symbol_id,
                finding_id=finding_id,
                analysis_run_id=analysis_run_id,
                diff_files=diff_files or [],
                config=config or ContextConfig(),
            )
        finally:
            snapshot.cleanup()

    async def build_context(
        self,
        *,
        repository_id: uuid.UUID,
        clone_url: str,
        commit_sha: str,
        repository_full_name: str,
        token: str | None = None,
        target_type: ContextTargetType,
        file_path: str,
        line: int | None = None,
        symbol_id: uuid.UUID | None = None,
        finding_id: uuid.UUID | None = None,
        analysis_run_id: uuid.UUID | None = None,
        diff_files: list[DiffFile] | None = None,
        config: ContextConfig | None = None,
    ) -> ContextBundle:
        """Build context for a repository fetched from ``clone_url``."""

        repository_index_id = await self._require_matching_index(
            repository_id=repository_id, commit_sha=commit_sha
        )
        with self._snapshot_provider.acquire(
            clone_url=clone_url,
            commit_sha=commit_sha,
            repository_full_name=repository_full_name,
            token=token,
        ) as snapshot:
            return await self._run(
                snapshot=snapshot,
                repository_id=repository_id,
                repository_index_id=repository_index_id,
                commit_sha=commit_sha,
                target_type=target_type,
                file_path=file_path,
                line=line,
                symbol_id=symbol_id,
                finding_id=finding_id,
                analysis_run_id=analysis_run_id,
                diff_files=diff_files or [],
                config=config or ContextConfig(),
            )

    async def _require_matching_index(
        self, *, repository_id: uuid.UUID, commit_sha: str
    ) -> uuid.UUID:
        index_repo = RepositoryIndexRepository()
        async with self._session_factory() as session:
            active_index = await index_repo.get_active(session, repository_id=repository_id)
        if active_index is None:
            raise StaleContextIndexError(f"no repository index exists for repository {repository_id}")
        if active_index.commit_sha != commit_sha:
            raise StaleContextIndexError(
                f"repository index is at commit {active_index.commit_sha!r}, "
                f"but context was requested for commit {commit_sha!r}"
            )
        return active_index.id

    async def _run(
        self,
        *,
        snapshot: RepositorySnapshot,
        repository_id: uuid.UUID,
        repository_index_id: uuid.UUID,
        commit_sha: str,
        target_type: ContextTargetType,
        file_path: str,
        line: int | None,
        symbol_id: uuid.UUID | None,
        finding_id: uuid.UUID | None,
        analysis_run_id: uuid.UUID | None,
        diff_files: list[DiffFile],
        config: ContextConfig,
    ) -> ContextBundle:
        start = time.monotonic()
        log = logger.bind(repository_id=str(repository_id), commit_sha=commit_sha)

        target = ContextTarget(
            repository_id=repository_id,
            repository_index_id=repository_index_id,
            commit_sha=commit_sha,
            target_type=target_type,
            file_path=file_path,
            line=line,
            symbol_id=symbol_id,
            finding_id=finding_id,
            analysis_run_id=analysis_run_id,
        )
        target_fingerprint = target.fingerprint()
        config_fingerprint = config.fingerprint()

        async with self._session_factory() as session:
            bundle, is_new = await self._bundle_repo.get_or_create_running(
                session,
                repository_id=repository_id,
                repository_index_id=repository_index_id,
                commit_sha=commit_sha,
                target_fingerprint=target_fingerprint,
                config_fingerprint=config_fingerprint,
                engine_version=CONTEXT_ENGINE_VERSION,
                target_type=target_type,
                target_file_path=file_path,
                target_line=line,
                target_symbol_id=symbol_id,
                finding_id=finding_id,
                analysis_run_id=analysis_run_id,
            )
            await session.commit()
            bundle_id = bundle.id

        if not is_new:
            log.info("context_bundle_reused", bundle_id=str(bundle_id))
            return await self._bundle_from_model(bundle, target=target, reused=True)

        try:
            result = await self._execute_and_persist(
                snapshot=snapshot,
                bundle_id=bundle_id,
                repository_id=repository_id,
                repository_index_id=repository_index_id,
                commit_sha=commit_sha,
                target=target,
                target_fingerprint=target_fingerprint,
                config=config,
                config_fingerprint=config_fingerprint,
                diff_files=diff_files,
                start=start,
                log=log,
            )
        except Exception as exc:
            async with self._session_factory() as session:
                await self._bundle_repo.mark_failed(session, bundle_id=bundle_id, error_message=str(exc))
                await session.commit()
            log.error("context_bundle_failed", bundle_id=str(bundle_id), error=str(exc))
            raise

        return result

    async def _execute_and_persist(
        self,
        *,
        snapshot: RepositorySnapshot,
        bundle_id: uuid.UUID,
        repository_id: uuid.UUID,
        repository_index_id: uuid.UUID,
        commit_sha: str,
        target: ContextTarget,
        target_fingerprint: str,
        config: ContextConfig,
        config_fingerprint: str,
        diff_files: list[DiffFile],
        start: float,
        log: structlog.stdlib.BoundLogger,
    ) -> ContextBundle:
        changed_lines_by_file = build_changed_lines_by_file(diff_files)

        async with self._session_factory() as session:
            target_file = await self._queries.get_file(
                session, repository_index_id=repository_index_id, relative_path=target.file_path
            )
            if target_file is None:
                raise ValueError(f"target file not indexed: {target.file_path!r}")

            target_symbol = None
            if target.symbol_id is not None:
                target_symbol = await self._queries.get_symbol_by_id(session, symbol_id=target.symbol_id)
            elif target.line is not None:
                target_symbol = await self._queries.symbol_containing_line(
                    session, indexed_file_id=target_file.id, line=target.line
                )

            finding_category = await self._finding_category(session, target.finding_id)

            adaptive_metrics: AdaptiveContextMetrics | None = None
            if config.adaptive is not None and config.adaptive.enabled:
                adaptive_result = await self._candidates.generate_adaptive(
                    session,
                    repository_index_id=repository_index_id,
                    target_file=target_file,
                    target_symbol=target_symbol,
                    target_line=target.line,
                    config=config,
                    changed_lines_by_file=changed_lines_by_file,
                    finding_category=finding_category,
                )
                candidates = adaptive_result.candidates
                decision: ExpansionDecision = adaptive_result.decision
                log.info(
                    "context_adaptive_decision",
                    expand=decision.expand,
                    reasons=[r.value for r in decision.reasons],
                    direction=decision.direction,
                )
            else:
                candidates = await self._candidates.generate(
                    session,
                    repository_index_id=repository_index_id,
                    target_file=target_file,
                    target_symbol=target_symbol,
                    target_line=target.line,
                    config=config,
                    changed_lines_by_file=changed_lines_by_file,
                )

        scored = []
        for c in candidates:
            item_score, breakdown = score_candidate(
                c, target_file_path=target.file_path, finding_category=finding_category
            )
            scored.append(ScoredCandidate(candidate=c, score=item_score, breakdown=breakdown))
        dedup_result = deduplicate(scored)

        max_expansion_tokens: int | None = None
        max_expansion_lines: int | None = None
        if config.adaptive is not None and config.adaptive.enabled:
            max_expansion_tokens = int(config.max_tokens * config.adaptive.expansion_token_fraction)
            max_expansion_lines = int(config.max_lines * config.adaptive.expansion_line_fraction)

        budget_result = self._budgeter.build(
            snapshot,
            kept=dedup_result.kept,
            max_tokens=config.max_tokens,
            max_lines=config.max_lines,
            max_tokens_per_item=config.max_tokens_per_item,
            max_lines_per_item=config.max_lines_per_item,
            target_reservation_fraction=config.target_reservation_fraction,
            max_items_per_relationship=config.max_items_per_relationship,
            max_expansion_tokens=max_expansion_tokens,
            max_expansion_lines=max_expansion_lines,
        )

        if config.adaptive is not None and config.adaptive.enabled:
            depth_2_selected_in_budget = sum(1 for i in budget_result.items if i.distance >= 2)
            depth_2_tokens = sum(i.estimated_tokens for i in budget_result.items if i.distance >= 2)
            adaptive_metrics = AdaptiveContextMetrics(
                attempted=True,
                occurred=depth_2_selected_in_budget > 0,
                reasons=decision.reasons,
                direction=decision.direction,
                requested_max_depth=config.adaptive.max_depth,
                effective_max_depth=2 if depth_2_selected_in_budget > 0 else 1,
                depth_2_candidate_count=adaptive_result.depth_2_candidate_count,
                depth_2_selected_count=depth_2_selected_in_budget,
                depth_2_tokens=depth_2_tokens,
            )
            log.info(
                "context_adaptive_expansion_completed",
                depth_2_candidates=adaptive_result.depth_2_candidate_count,
                depth_2_selected=depth_2_selected_in_budget,
                depth_2_tokens=depth_2_tokens,
            )

        duration_ms = (time.monotonic() - start) * 1000

        async with self._session_factory() as session:
            existing = await self._bundle_repo.claim_for_write(
                session,
                bundle_id=bundle_id,
                repository_id=repository_id,
                commit_sha=commit_sha,
                target_fingerprint=target_fingerprint,
                config_fingerprint=config_fingerprint,
            )
            if existing is not None:
                await self._bundle_repo.mark_failed(
                    session, bundle_id=bundle_id, error_message=f"superseded by concurrent context bundle {existing.id}"
                )
                await session.commit()
                log.info("context_bundle_superseded", bundle_id=str(bundle_id), winning_bundle_id=str(existing.id))
                return await self._bundle_from_model(existing, target=target, reused=True)

            item_models = _build_item_models(bundle_id, budget_result.items)
            await self._item_repo.bulk_create(session, item_models)

            final_bundle = await self._bundle_repo.mark_succeeded(
                session,
                bundle_id=bundle_id,
                candidate_count=len(candidates),
                selected_count=len(budget_result.items),
                dropped_budget=budget_result.dropped_budget,
                dropped_overlap=dedup_result.dropped_overlap,
                dropped_duplicate=dedup_result.dropped_duplicate,
                total_tokens=budget_result.total_tokens,
                total_lines=budget_result.total_lines,
                generation_ms=duration_ms,
                adaptive_metrics=adaptive_metrics,
            )
            await session.commit()

        log.info(
            "context_bundle_completed",
            bundle_id=str(bundle_id),
            candidate_count=len(candidates),
            selected_count=len(budget_result.items),
            dropped_budget=budget_result.dropped_budget,
            dropped_overlap=dedup_result.dropped_overlap,
            dropped_duplicate=dedup_result.dropped_duplicate,
            total_tokens=budget_result.total_tokens,
            total_lines=budget_result.total_lines,
            duration_ms=duration_ms,
        )

        return ContextBundle(
            id=final_bundle.id,
            target=target,
            items=budget_result.items,
            total_tokens_estimate=budget_result.total_tokens,
            total_lines=budget_result.total_lines,
            config_fingerprint=config_fingerprint,
            generation_version=CONTEXT_ENGINE_VERSION,
            metrics=ContextQualityMetrics(
                candidate_count=len(candidates),
                selected_count=len(budget_result.items),
                dropped_budget=budget_result.dropped_budget,
                dropped_overlap=dedup_result.dropped_overlap,
                dropped_duplicate=dedup_result.dropped_duplicate,
                total_tokens=budget_result.total_tokens,
                total_lines=budget_result.total_lines,
                generation_ms=duration_ms,
            ),
            reused_existing_bundle=(final_bundle.id != bundle_id),
            adaptive_metrics=adaptive_metrics,
        )

    async def _finding_category(
        self, session: AsyncSession, finding_id: uuid.UUID | None
    ) -> FindingCategory | None:
        if finding_id is None:
            return None
        finding = await session.get(FindingModel, finding_id)
        return finding.category if finding is not None else None

    async def _bundle_from_model(
        self, bundle: ContextBundleModel, *, target: ContextTarget, reused: bool
    ) -> ContextBundle:
        async with self._session_factory() as session:
            item_models = await self._item_repo.list_for_bundle(session, bundle_id=bundle.id)
        items = tuple(_item_from_model(m) for m in item_models)
        return ContextBundle(
            id=bundle.id,
            target=target,
            items=items,
            total_tokens_estimate=bundle.total_tokens,
            total_lines=bundle.total_lines,
            config_fingerprint=bundle.config_fingerprint,
            generation_version=bundle.engine_version,
            metrics=ContextQualityMetrics(
                candidate_count=bundle.candidate_count,
                selected_count=bundle.selected_count,
                dropped_budget=bundle.dropped_budget,
                dropped_overlap=bundle.dropped_overlap,
                dropped_duplicate=bundle.dropped_duplicate,
                total_tokens=bundle.total_tokens,
                total_lines=bundle.total_lines,
                generation_ms=bundle.generation_ms or 0.0,
            ),
            reused_existing_bundle=reused,
            adaptive_metrics=_adaptive_metrics_from_model(bundle),
        )


def _build_item_models(bundle_id: uuid.UUID, items: tuple[ContextItem, ...]) -> list[ContextItemModel]:
    models = []
    for rank, item in enumerate(items):
        content_hash = hashlib.sha256(item.content.encode()).hexdigest()
        models.append(
            ContextItemModel(
                bundle_id=bundle_id,
                rank=rank,
                kind=item.kind,
                relationship=item.relationship,
                distance=item.distance,
                file_path=item.file_path,
                symbol_id=item.symbol_id,
                symbol_name=item.symbol_name,
                qualified_name=item.qualified_name,
                start_line=item.start_line,
                end_line=item.end_line,
                content=item.content,
                content_hash=content_hash,
                truncated=item.truncated,
                score=item.score,
                score_breakdown=json.dumps([{"label": c.label, "value": c.value} for c in item.score_breakdown]),
                estimated_tokens=item.estimated_tokens,
                reason=item.reason,
            )
        )
    return models


def _item_from_model(model: ContextItemModel) -> ContextItem:
    breakdown = tuple(
        ScoreComponent(label=c["label"], value=c["value"]) for c in json.loads(model.score_breakdown)
    )
    return ContextItem(
        kind=model.kind,
        file_path=model.file_path,
        symbol_id=model.symbol_id,
        symbol_name=model.symbol_name,
        qualified_name=model.qualified_name,
        start_line=model.start_line,
        end_line=model.end_line,
        content=model.content,
        relationship=model.relationship,
        distance=model.distance,
        score=model.score,
        score_breakdown=breakdown,
        estimated_tokens=model.estimated_tokens,
        reason=model.reason,
        truncated=model.truncated,
    )


def _adaptive_metrics_from_model(bundle: ContextBundleModel) -> AdaptiveContextMetrics | None:
    if not bundle.adaptive_expansion_attempted:
        return None
    return AdaptiveContextMetrics(
        attempted=True,
        occurred=bundle.adaptive_expansion_occurred,
        reasons=tuple(ExpansionReason(r) for r in json.loads(bundle.adaptive_expansion_reasons or "[]")),
        direction=bundle.adaptive_expansion_direction,  # type: ignore[arg-type]
        requested_max_depth=bundle.adaptive_requested_max_depth or 0,
        effective_max_depth=bundle.adaptive_effective_max_depth or 0,
        depth_2_candidate_count=bundle.depth_2_candidate_count,
        depth_2_selected_count=bundle.depth_2_selected_count,
        depth_2_tokens=bundle.depth_2_tokens,
    )
