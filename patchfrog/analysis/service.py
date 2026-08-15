"""Static analysis orchestration.

Wires together: stale-index protection -> snapshot acquisition -> config
loading -> idempotency/concurrency-safe run creation -> analyzer selection
-> concurrent execution -> repository-intelligence enrichment ->
deduplication -> persistence, as one analysis run tied to a commit SHA.

Transaction strategy mirrors Phase 2's indexing service: creating the
``analysis_runs`` row (status ``running``) is committed immediately so
it's observable while analyzers run; every row the pipeline produces is
then inserted in a single second transaction that only commits once
everything has succeeded (or partially succeeded — see
:class:`~patchfrog.persistence.models.analysis.AnalysisRunStatus`). Any
exception before that commit leaves nothing persisted beyond the
``running`` row, which a third, independent transaction marks ``failed``.
"""

from __future__ import annotations

import json
import time
import uuid
from pathlib import Path

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from patchfrog.analysis.analyzers.base import Analyzer
from patchfrog.analysis.analyzers.registry import AnalyzerRegistry, default_registry
from patchfrog.analysis.changed_lines import build_changed_lines_by_file
from patchfrog.analysis.config import AnalysisConfig, load_analysis_config
from patchfrog.analysis.dedupe import DeduplicatedFinding, deduplicate
from patchfrog.analysis.domain import (
    AnalysisContext,
    AnalysisRunResultStatus,
    AnalysisRunSummary,
    AnalyzerExecutionStatus,
    AnalyzerResult,
    RawFinding,
)
from patchfrog.analysis.enrichment import FindingEnricher
from patchfrog.analysis.execution import run_analyzers
from patchfrog.analysis.selection import select_analyzers
from patchfrog.analysis.toolchain import discover_toolchain
from patchfrog.diff.models import DiffFile
from patchfrog.domain.code import Language
from patchfrog.parsing.detect import detect_language
from patchfrog.persistence.models.analysis import (
    AnalysisRunModel,
    AnalysisRunStatus,
    AnalyzerExecutionModel,
    FindingModel,
    FindingSourceModel,
)
from patchfrog.persistence.repositories import (
    AnalysisRunRepository,
    AnalyzerExecutionRepository,
    FindingRepository,
    FindingSourceRepository,
    IndexedFileRepository,
    RepositoryIndexRepository,
)
from patchfrog.repository.snapshot import RepositorySnapshot, RepositorySnapshotProvider

logger = structlog.get_logger(__name__)


class StaleIndexError(RuntimeError):
    """No Phase 2 repository index exists for the exact commit being analyzed.

    Enforces the mandatory invariant that analysis is only ever performed
    against repository intelligence for the *same* commit — never a
    stale or differently-versioned index.
    """


class StaticAnalysisService:
    """Orchestrates one static analysis run of a repository."""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        snapshot_provider: RepositorySnapshotProvider | None = None,
        analyzer_registry: AnalyzerRegistry | None = None,
        enricher: FindingEnricher | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._snapshot_provider = snapshot_provider or RepositorySnapshotProvider()
        self._registry = analyzer_registry or default_registry()
        self._enricher = enricher or FindingEnricher()
        self._run_repo = AnalysisRunRepository()
        self._index_repo = RepositoryIndexRepository()
        self._file_repo = IndexedFileRepository()
        self._execution_repo = AnalyzerExecutionRepository()
        self._finding_repo = FindingRepository()
        self._source_repo = FindingSourceRepository()

    async def analyze_repository(
        self,
        *,
        repository_id: uuid.UUID,
        clone_url: str,
        commit_sha: str,
        repository_full_name: str,
        token: str | None = None,
        pull_request_id: uuid.UUID | None = None,
        diff_files: list[DiffFile] | None = None,
    ) -> AnalysisRunSummary:
        """Analyze a repository fetched from ``clone_url`` at ``commit_sha``."""

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
                pull_request_id=pull_request_id,
                diff_files=diff_files or [],
            )

    async def analyze_local_repository(
        self,
        *,
        repository_id: uuid.UUID,
        root_path: Path,
        repository_full_name: str,
        pull_request_id: uuid.UUID | None = None,
        diff_files: list[DiffFile] | None = None,
    ) -> AnalysisRunSummary:
        """Analyze a repository already checked out on disk (developer/CLI use)."""

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
                pull_request_id=pull_request_id,
                diff_files=diff_files or [],
            )
        finally:
            snapshot.cleanup()

    async def _require_matching_index(
        self, *, repository_id: uuid.UUID, commit_sha: str
    ) -> uuid.UUID:
        async with self._session_factory() as session:
            active_index = await self._index_repo.get_active(session, repository_id=repository_id)
        if active_index is None:
            raise StaleIndexError(f"no repository index exists for repository {repository_id}")
        if active_index.commit_sha != commit_sha:
            raise StaleIndexError(
                f"repository index is at commit {active_index.commit_sha!r}, "
                f"but analysis was requested for commit {commit_sha!r}"
            )
        return active_index.id

    async def _run(
        self,
        *,
        snapshot: RepositorySnapshot,
        repository_id: uuid.UUID,
        repository_index_id: uuid.UUID,
        commit_sha: str,
        pull_request_id: uuid.UUID | None,
        diff_files: list[DiffFile],
    ) -> AnalysisRunSummary:
        start = time.monotonic()
        config = load_analysis_config(snapshot.root_path)
        config_fingerprint = config.fingerprint()
        log = logger.bind(repository_id=str(repository_id), commit_sha=commit_sha)

        changed_files = frozenset(d.path for d in diff_files)
        changed_lines_by_file = build_changed_lines_by_file(diff_files)
        languages = await self._determine_languages(
            changed_files=changed_files, repository_index_id=repository_index_id
        )
        selected = select_analyzers(self._registry, config=config, languages=languages)

        # Discovered *before* the idempotency check, not just before
        # execution: the effective toolchain (analyzer versions, bundled
        # ruleset content, engine version) is part of the run's identity,
        # not only something recorded after the fact -- reusing a prior
        # canonical run must depend on knowing whether today's toolchain
        # still matches it, which requires discovering it up front. This
        # is the same cheap `--version`-only discovery each adapter's own
        # `analyze()` does internally, just run once, concurrently, ahead
        # of time.
        toolchain = await discover_toolchain(selected)
        toolchain_fingerprint = toolchain.fingerprint()

        async with self._session_factory() as session:
            run, is_new = await self._run_repo.get_or_create_running(
                session,
                repository_id=repository_id,
                repository_index_id=repository_index_id,
                commit_sha=commit_sha,
                config_fingerprint=config_fingerprint,
                toolchain_fingerprint=toolchain_fingerprint,
                pull_request_id=pull_request_id,
            )
            await session.commit()
            run_id = run.id

        if not is_new:
            log.info("analysis_run_reused", run_id=str(run_id))
            return _summary_from_run(run, reused_existing_run=True)

        try:
            summary = await self._execute_and_persist(
                session_factory=self._session_factory,
                snapshot=snapshot,
                run_id=run_id,
                repository_id=repository_id,
                repository_index_id=repository_index_id,
                config=config,
                config_fingerprint=config_fingerprint,
                toolchain_fingerprint=toolchain_fingerprint,
                selected=selected,
                languages=languages,
                changed_files=changed_files,
                changed_lines_by_file=changed_lines_by_file,
                start=start,
                log=log,
            )
        except Exception as exc:
            async with self._session_factory() as session:
                await self._run_repo.mark_failed(session, run_id=run_id, error_message=str(exc))
                await session.commit()
            log.error("analysis_run_failed", run_id=str(run_id), error=str(exc))
            raise

        return summary

    async def _execute_and_persist(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        snapshot: RepositorySnapshot,
        run_id: uuid.UUID,
        repository_id: uuid.UUID,
        repository_index_id: uuid.UUID,
        config: AnalysisConfig,
        config_fingerprint: str,
        toolchain_fingerprint: str,
        selected: dict[str, Analyzer],
        languages: frozenset[Language],
        changed_files: frozenset[str],
        changed_lines_by_file: dict[str, frozenset[int]],
        start: float,
        log: structlog.stdlib.BoundLogger,
    ) -> AnalysisRunSummary:
        context = AnalysisContext(
            repository_id=repository_id,
            repository_index_id=repository_index_id,
            commit_sha=snapshot.commit_sha,
            checkout_path=snapshot.root_path,
            pull_request_number=None,
            changed_files=changed_files,
            changed_lines_by_file=changed_lines_by_file,
            languages=languages,
            config=config,
        )

        analyzer_results = await run_analyzers(selected, context)

        raw_findings: list[RawFinding] = []
        for result in analyzer_results.values():
            raw_findings.extend(result.findings)

        async with session_factory() as session:
            existing = await self._run_repo.claim_for_write(
                session,
                run_id=run_id,
                repository_id=repository_id,
                commit_sha=snapshot.commit_sha,
                config_fingerprint=config_fingerprint,
                toolchain_fingerprint=toolchain_fingerprint,
            )
            if existing is not None:
                await self._run_repo.mark_failed(
                    session,
                    run_id=run_id,
                    error_message=f"superseded by concurrent analysis run {existing.id}",
                )
                await session.commit()
                log.info(
                    "analysis_run_superseded",
                    run_id=str(run_id),
                    winning_run_id=str(existing.id),
                )
                return _summary_from_run(existing, reused_existing_run=True)

            enriched = await self._enricher.enrich_many(
                session,
                findings=raw_findings,
                repository_index_id=repository_index_id,
                changed_lines_by_file=changed_lines_by_file,
            )
            deduped = deduplicate(enriched)

            execution_models = [
                _build_execution_model(run_id, name, result) for name, result in analyzer_results.items()
            ]
            await self._execution_repo.bulk_create(session, execution_models)

            finding_models = _build_finding_models(run_id, deduped)
            await self._finding_repo.bulk_create(session, finding_models)  # flush -> ids assigned

            source_models = _build_finding_source_models(finding_models, deduped)
            await self._source_repo.bulk_create(session, source_models)

            succeeded = sum(
                1 for r in analyzer_results.values() if r.status is AnalyzerExecutionStatus.SUCCEEDED
            )
            failed = sum(
                1
                for r in analyzer_results.values()
                if r.status in (AnalyzerExecutionStatus.FAILED, AnalyzerExecutionStatus.TIMED_OUT)
            )
            skipped = sum(
                1
                for r in analyzer_results.values()
                if r.status in (AnalyzerExecutionStatus.SKIPPED, AnalyzerExecutionStatus.UNSUPPORTED)
            )

            if failed == 0:
                run_status = AnalysisRunStatus.SUCCEEDED
            elif succeeded > 0:
                run_status = AnalysisRunStatus.PARTIAL
            else:
                run_status = AnalysisRunStatus.FAILED

            duration_ms = (time.monotonic() - start) * 1000
            final_run = await self._run_repo.mark_succeeded(
                session,
                run_id=run_id,
                status=run_status,
                analyzers_succeeded=succeeded,
                analyzers_failed=failed,
                analyzers_skipped=skipped,
                raw_findings_count=len(raw_findings),
                findings_count=len(finding_models),
                duration_ms=duration_ms,
            )
            await session.commit()

        log.info(
            "analysis_run_completed",
            run_id=str(run_id),
            status=final_run.status.value,
            analyzers_succeeded=final_run.analyzers_succeeded,
            analyzers_failed=final_run.analyzers_failed,
            analyzers_skipped=final_run.analyzers_skipped,
            raw_findings_count=final_run.raw_findings_count,
            findings_count=final_run.findings_count,
            duration_ms=final_run.duration_ms,
        )

        return _summary_from_run(final_run, reused_existing_run=(final_run.id != run_id))

    async def _determine_languages(
        self, *, changed_files: frozenset[str], repository_index_id: uuid.UUID
    ) -> frozenset[Language]:
        if changed_files:
            detected = (detect_language(relative_path=path) for path in changed_files)
            return frozenset(lang for lang in detected if lang is not None)
        async with self._session_factory() as session:
            return await self._file_repo.distinct_languages(session, repository_index_id=repository_index_id)


def _build_execution_model(run_id: uuid.UUID, analyzer: str, result: AnalyzerResult) -> AnalyzerExecutionModel:
    return AnalyzerExecutionModel(
        analysis_run_id=run_id,
        analyzer=analyzer,
        version=result.version,
        status=result.status,
        duration_ms=result.duration_ms,
        raw_findings_count=len(result.findings),
        error_message=result.error,
    )


def _build_finding_models(run_id: uuid.UUID, deduped: list[DeduplicatedFinding]) -> list[FindingModel]:
    finding_models: list[FindingModel] = []
    for group in deduped:
        primary = group.primary
        finding_models.append(
            FindingModel(
                analysis_run_id=run_id,
                fingerprint=primary.fingerprint,
                rule_id=primary.finding.rule_id,
                category=primary.finding.category,
                title=primary.finding.title,
                message=primary.finding.message,
                severity=primary.finding.severity,
                confidence=primary.finding.confidence,
                file_path=primary.finding.file_path,
                start_line=primary.finding.span.start_line,
                end_line=primary.finding.span.end_line,
                start_column=primary.finding.span.start_column,
                end_column=primary.finding.span.end_column,
                symbol_id=primary.symbol_id,
                symbol_name=primary.symbol_name,
                source_analyzer=primary.finding.source_analyzer,
                is_on_changed_line=primary.is_on_changed_line,
                is_in_changed_symbol=primary.is_in_changed_symbol,
                raw_metadata=json.dumps(primary.finding.raw_metadata) if primary.finding.raw_metadata else None,
            )
        )
    return finding_models


def _build_finding_source_models(
    finding_models: list[FindingModel], deduped: list[DeduplicatedFinding]
) -> list[FindingSourceModel]:
    """Must run *after* ``finding_models`` has been flushed — each model's
    ``id`` is a Python-side default only assigned by SQLAlchemy at flush
    time, not at construction."""

    source_models: list[FindingSourceModel] = []
    for finding_model, group in zip(finding_models, deduped, strict=True):
        for source in group.reported_by:
            source_models.append(
                FindingSourceModel(
                    finding_id=finding_model.id,
                    analyzer=source.finding.source_analyzer,
                    rule_id=source.finding.rule_id,
                    fingerprint=source.fingerprint,
                    severity=source.finding.severity,
                    confidence=source.finding.confidence,
                    message=source.finding.message,
                    start_line=source.finding.span.start_line,
                    end_line=source.finding.span.end_line,
                    start_column=source.finding.span.start_column,
                    end_column=source.finding.span.end_column,
                )
            )
    return source_models


def _summary_from_run(run: AnalysisRunModel, *, reused_existing_run: bool) -> AnalysisRunSummary:
    return AnalysisRunSummary(
        status=AnalysisRunResultStatus(run.status.value),
        analyzers_succeeded=run.analyzers_succeeded,
        analyzers_failed=run.analyzers_failed,
        analyzers_skipped=run.analyzers_skipped,
        raw_findings_count=run.raw_findings_count,
        findings_count=run.findings_count,
        duration_ms=run.duration_ms or 0.0,
        reused_existing_run=reused_existing_run,
    )
