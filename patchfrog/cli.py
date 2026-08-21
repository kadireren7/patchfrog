"""Developer CLI for repository indexing, static analysis, and context.

    python -m patchfrog.cli index --repository /path/to/repo [--full-name owner/repo]
    python -m patchfrog.cli analyze --repository /path/to/repo [--full-name owner/repo]
    python -m patchfrog.cli context --repository /path/to/repo --finding-id <id>
    python -m patchfrog.cli context --repository /path/to/repo --file src/foo.py --line 42
    python -m patchfrog.cli review --repository /path/to/repo --base main [--dry-run]
    python -m patchfrog.cli publish --review-run-id <id> [--publish]
    python -m patchfrog.cli eval run [--mode full_pipeline] [--tag T] [--critic on|off|both]
    python -m patchfrog.cli eval compare [--baseline PATH] [--current PATH]
    python -m patchfrog.cli eval report --input PATH
    python -m patchfrog.cli eval update-baseline --confirm [--input PATH]

Deliberately minimal — a couple of subcommands, argparse only. This
exists purely as a controlled way to trigger indexing/analysis/context
generation during development and self-validation; it is not a
general-purpose CLI framework.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import sys
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from patchfrog.analysis.domain import AnalysisRunSummary
from patchfrog.analysis.service import StaleIndexError, StaticAnalysisService
from patchfrog.config.logging import configure_logging
from patchfrog.config.settings import Settings, get_settings
from patchfrog.context.config import ContextConfig
from patchfrog.context.domain import ContextBundle, ContextItemKind, ContextTargetType
from patchfrog.context.service import ContextService, StaleContextIndexError
from patchfrog.evaluation.domain import (
    CaseStatus,
    EvaluationCase,
    EvaluationMode,
    EvaluationRunResult,
    IncrementalScenarioResult,
)
from patchfrog.evaluation.fixtures import (
    DEFAULT_CASES_ROOT,
    fixture_info_for_case,
    load_all_cases,
    validate_and_raise,
)
from patchfrog.evaluation.fixtures import (
    BenchmarkValidationError as EvalBenchmarkValidationError,
)
from patchfrog.evaluation.incremental import run_all_incremental_scenarios
from patchfrog.evaluation.metrics import compute_critic_comparison
from patchfrog.evaluation.queries import DEFAULT_BASELINES_ROOT, default_baseline_path
from patchfrog.evaluation.regression import RegressionThresholds
from patchfrog.evaluation.regression import compare as compare_evaluation_runs
from patchfrog.evaluation.reporting import build_report, read_json, render_markdown, write_json
from patchfrog.evaluation.runner import (
    EvaluationRunner,
    build_evaluation_identity,
    oracle_reviewer_provider_factory,
)
from patchfrog.github.auth import InstallationTokenProvider
from patchfrog.github.client import GitHubClient
from patchfrog.indexing.models import IndexingSummary
from patchfrog.indexing.service import RepositoryIndexingService
from patchfrog.persistence.database import create_engine, create_session_factory
from patchfrog.persistence.models.analysis import FindingModel
from patchfrog.persistence.models.pull_request import PullRequestModel
from patchfrog.persistence.models.repository import RepositoryModel
from patchfrog.persistence.models.review import ReviewRunModel
from patchfrog.persistence.models.review_memory import ReviewGenerationModel
from patchfrog.persistence.repositories import PullRequestRepository, RepositoryRepository
from patchfrog.persistence.repositories.repository_index import RepositoryIndexRepository
from patchfrog.publishing.config_resolution import resolve_repository_publication_config
from patchfrog.publishing.domain import ReviewPublicationMode, ReviewPublicationResult
from patchfrog.publishing.github_publisher import GitHubClientReviewPublisher
from patchfrog.publishing.service import (
    ReviewNotFoundError,
    ReviewPublicationService,
    ReviewRunNotAssociatedWithPullRequestError,
)
from patchfrog.repository.git import GitError, run_git
from patchfrog.review.candidates import ReviewCandidateGenerator
from patchfrog.review.config import MalformedReviewConfigError, ReviewConfig
from patchfrog.review.config_resolution import resolve_repository_review_config
from patchfrog.review.domain import ReviewCandidate, ReviewRunSummary
from patchfrog.review.local_diff import diff_against_base
from patchfrog.review.provider import LLMProvider
from patchfrog.review.provider_factory import (
    MissingProviderCredentialsError,
    build_critic_provider,
    build_reviewer_provider,
)
from patchfrog.review.service import (
    PullRequestReviewService,
    StaleReviewIndexError,
    persist_malformed_config_failure,
)
from patchfrog.review_memory.config_resolution import resolve_repository_incremental_config
from patchfrog.review_memory.domain import IncrementalPlan, ReviewMemoryFinding
from patchfrog.review_memory.queries import ReviewMemoryQueryService
from patchfrog.review_memory.service import IncrementalReviewMemoryService

logger = structlog.get_logger(__name__)


def _synthetic_github_repository_id(full_name: str) -> int:
    """A stable, non-negative id for a repository with no real GitHub App installation.

    CLI-indexed repositories aren't necessarily backed by a GitHub App
    installation (e.g. indexing a scratch checkout), but ``repositories``
    still requires a unique ``github_repository_id`` — derived
    deterministically from the repository's full name so repeat CLI runs
    against the same repository reuse the same row (and therefore benefit
    from incremental indexing).
    """

    digest = hashlib.sha256(full_name.encode()).digest()[:8]
    return int.from_bytes(digest, "big") & 0x7FFFFFFFFFFFFFFF


def _default_full_name(repository_path: Path) -> str:
    return repository_path.resolve().name


async def _upsert_cli_repository(
    session_factory: async_sessionmaker[AsyncSession], *, full_name: str
) -> uuid.UUID:
    owner, _, name = full_name.partition("/")
    async with session_factory() as session:
        repository_row = await RepositoryRepository().upsert(
            session,
            github_repository_id=_synthetic_github_repository_id(full_name),
            owner=owner or full_name,
            name=name or full_name,
            full_name=full_name,
            installation_id=0,
        )
        await session.commit()
        return repository_row.id


#: The one synthetic "local PR" review memory is scoped to for CLI
#: ``--incremental`` use -- there is no real GitHub pull request behind a
#: bare local checkout, so (mirroring :func:`_synthetic_github_repository_id`'s
#: "stable synthetic identity for CLI use" approach) every ``--incremental``
#: CLI invocation against the same repository/full-name is treated as
#: iterating on the same one PR, exactly matching how a developer actually
#: uses this locally: successive commits on the same branch.
_CLI_SYNTHETIC_PR_NUMBER = 0


async def _upsert_cli_pull_request(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    repository_id: uuid.UUID,
    base_sha: str,
    head_sha: str,
) -> uuid.UUID:
    async with session_factory() as session:
        pr = await PullRequestRepository().upsert(
            session,
            repository_id=repository_id,
            github_pr_number=_CLI_SYNTHETIC_PR_NUMBER,
            title="local --incremental checkout",
            author="cli",
            base_sha=base_sha,
            head_sha=head_sha,
            state="open",
        )
        await session.commit()
        return pr.id


async def _index_local(*, repository_path: Path, full_name: str) -> IndexingSummary:
    settings = get_settings()
    configure_logging(settings.log_level)

    engine = create_engine(settings.database_url)
    session_factory = create_session_factory(engine)
    try:
        repository_id = await _upsert_cli_repository(session_factory, full_name=full_name)

        service = RepositoryIndexingService(session_factory=session_factory)
        return await service.index_local_repository(
            repository_id=repository_id,
            root_path=repository_path,
            repository_full_name=full_name,
        )
    finally:
        await engine.dispose()


async def _analyze_local(*, repository_path: Path, full_name: str) -> AnalysisRunSummary:
    settings = get_settings()
    configure_logging(settings.log_level)

    engine = create_engine(settings.database_url)
    session_factory = create_session_factory(engine)
    try:
        repository_id = await _upsert_cli_repository(session_factory, full_name=full_name)

        service = StaticAnalysisService(session_factory=session_factory)
        return await service.analyze_local_repository(
            repository_id=repository_id,
            root_path=repository_path,
            repository_full_name=full_name,
        )
    finally:
        await engine.dispose()


async def _context_local(
    *, repository_path: Path, full_name: str, finding_id: uuid.UUID | None, file_path: str | None, line: int | None
) -> ContextBundle:
    settings = get_settings()
    configure_logging(settings.log_level)

    engine = create_engine(settings.database_url)
    session_factory = create_session_factory(engine)
    try:
        repository_id = await _upsert_cli_repository(session_factory, full_name=full_name)
        service = ContextService(session_factory=session_factory)

        if finding_id is not None:
            async with session_factory() as session:
                finding = await session.get(FindingModel, finding_id)
            if finding is None:
                raise ValueError(f"no finding with id {finding_id}")
            return await service.build_context_local(
                repository_id=repository_id,
                root_path=repository_path,
                repository_full_name=full_name,
                target_type=ContextTargetType.FINDING,
                file_path=finding.file_path,
                line=finding.start_line,
                symbol_id=finding.symbol_id,
                finding_id=finding.id,
                analysis_run_id=finding.analysis_run_id,
            )

        assert file_path is not None and line is not None  # enforced by argparse mutual-exclusion below
        return await service.build_context_local(
            repository_id=repository_id,
            root_path=repository_path,
            repository_full_name=full_name,
            target_type=ContextTargetType.LINE,
            file_path=file_path,
            line=line,
        )
    finally:
        await engine.dispose()


async def _review_dry_run(
    *, repository_path: Path, full_name: str, base_ref: str, incremental: bool
) -> tuple[ReviewConfig, tuple[ReviewCandidate, ...], IncrementalPlan | None]:
    """Build review candidates (and, implicitly, the context each would
    use) without ever constructing a provider or making a network call --
    the safe path required before anyone runs a real, billed review.

    ``incremental`` additionally builds (but never persists) the Phase 7
    incremental plan against a synthetic local "PR" scoped to this
    repository (see :func:`_upsert_cli_pull_request`) -- still zero
    provider calls, ancestry verification is real git plumbing against
    the local checkout itself.
    """

    settings = get_settings()
    configure_logging(settings.log_level)

    engine = create_engine(settings.database_url)
    session_factory = create_session_factory(engine)
    try:
        repository_id = await _upsert_cli_repository(session_factory, full_name=full_name)
        commit_sha = run_git(["rev-parse", "HEAD"], cwd=repository_path).strip()
        config = await resolve_repository_review_config(
            local=True, commit_sha=commit_sha, repository_full_name=full_name, root_path=repository_path
        )
        diff_files = diff_against_base(repository_path, base_ref)

        async with session_factory() as session:
            active_index = await RepositoryIndexRepository().get_active(session, repository_id=repository_id)
            if active_index is None:
                raise StaleReviewIndexError(f"no repository index exists for repository {repository_id}")

            static_findings: list[FindingModel] = []
            candidates = await ReviewCandidateGenerator().generate(
                session,
                repository_index_id=active_index.id,
                diff_files=diff_files,
                static_findings=static_findings,
                max_candidates=config.max_candidates,
            )

        if not incremental:
            return config, candidates, None

        base_sha = run_git(["rev-parse", base_ref], cwd=repository_path).strip()
        pull_request_id = await _upsert_cli_pull_request(
            session_factory, repository_id=repository_id, base_sha=base_sha, head_sha=commit_sha
        )
        incremental_config = await resolve_repository_incremental_config(
            local=True, commit_sha=commit_sha, repository_full_name=full_name, root_path=repository_path
        )
        memory_service = IncrementalReviewMemoryService(session_factory=session_factory)
        prepared = await memory_service.prepare(
            pull_request_id=pull_request_id,
            repository_index_id=active_index.id,
            commit_sha=commit_sha,
            clone_url=str(repository_path),
            token=None,
            current_candidates=candidates,
            reviewer_provider=config.provider,
            reviewer_model=config.model,
            incremental_config=incremental_config,
        )
        return config, candidates, prepared.plan
    finally:
        await engine.dispose()


async def _review_local(
    *, repository_path: Path, full_name: str, base_ref: str, incremental: bool
) -> ReviewRunSummary:
    settings = get_settings()
    configure_logging(settings.log_level)

    engine = create_engine(settings.database_url)
    session_factory = create_session_factory(engine)
    try:
        repository_id = await _upsert_cli_repository(session_factory, full_name=full_name)
        commit_sha = run_git(["rev-parse", "HEAD"], cwd=repository_path).strip()
        try:
            config = await resolve_repository_review_config(
                local=True, commit_sha=commit_sha, repository_full_name=full_name, root_path=repository_path
            )
        except MalformedReviewConfigError as exc:
            await persist_malformed_config_failure(
                session_factory,
                repository_id=repository_id,
                commit_sha=commit_sha,
                pull_request_id=None,
                exc=exc,
            )
            raise
        diff_files = diff_against_base(repository_path, base_ref)

        reviewer_provider = build_reviewer_provider(config, settings=settings)
        critic_provider = build_critic_provider(config, settings=settings)

        service = PullRequestReviewService(
            session_factory=session_factory,
            reviewer_provider=reviewer_provider,
            critic_provider=critic_provider,
        )

        if not incremental:
            return await service.review_local(
                repository_id=repository_id,
                root_path=repository_path,
                repository_full_name=full_name,
                commit_sha=commit_sha,
                diff_files=diff_files,
                config=config,
            )

        async with session_factory() as session:
            active_index = await RepositoryIndexRepository().get_active(session, repository_id=repository_id)
            if active_index is None:
                raise StaleReviewIndexError(f"no repository index exists for repository {repository_id}")

        base_sha = run_git(["rev-parse", base_ref], cwd=repository_path).strip()
        pull_request_id = await _upsert_cli_pull_request(
            session_factory, repository_id=repository_id, base_sha=base_sha, head_sha=commit_sha
        )
        incremental_config = await resolve_repository_incremental_config(
            local=True, commit_sha=commit_sha, repository_full_name=full_name, root_path=repository_path
        )
        memory_service = IncrementalReviewMemoryService(session_factory=session_factory)
        full_candidates = await memory_service.build_candidates(
            repository_id=repository_id,
            repository_index_id=active_index.id,
            commit_sha=commit_sha,
            diff_files=diff_files,
            max_candidates=config.max_candidates,
        )
        prepared = await memory_service.prepare(
            pull_request_id=pull_request_id,
            repository_index_id=active_index.id,
            commit_sha=commit_sha,
            clone_url=str(repository_path),
            token=None,
            current_candidates=full_candidates,
            reviewer_provider=reviewer_provider.identity.provider,
            reviewer_model=reviewer_provider.identity.model,
            incremental_config=incremental_config,
        )
        summary = await service.review_local(
            repository_id=repository_id,
            root_path=repository_path,
            repository_full_name=full_name,
            commit_sha=commit_sha,
            diff_files=diff_files,
            pull_request_id=pull_request_id,
            config=config,
            candidate_filter=prepared.candidate_filter,
            incremental_context_fingerprint=prepared.incremental_context_fingerprint,
        )
        if prepared.memory_tracking_active:
            await memory_service.finalize(
                review_run_id=summary.run_id,
                repository_id=repository_id,
                pull_request_id=pull_request_id,
                commit_sha=commit_sha,
                prepared=prepared,
            )
        return summary
    finally:
        await engine.dispose()


@dataclass(slots=True)
class ReviewHistoryReport:
    generations: list[ReviewGenerationModel]
    open_findings: list[ReviewMemoryFinding]
    transitions_by_generation: dict[uuid.UUID, int]


async def _review_history(*, repository_path: Path, full_name: str) -> ReviewHistoryReport | None:
    """Read-only inspection of Phase 7 state for the CLI's synthetic
    local PR (see :func:`_upsert_cli_pull_request`) -- never writes
    anything, never verifies ancestry, never calls a provider."""

    settings = get_settings()
    configure_logging(settings.log_level)

    engine = create_engine(settings.database_url)
    session_factory = create_session_factory(engine)
    try:
        repository_id = await _upsert_cli_repository(session_factory, full_name=full_name)
        async with session_factory() as session:
            pr = await PullRequestRepository().get_by_repository_and_number(
                session, repository_id=repository_id, github_pr_number=_CLI_SYNTHETIC_PR_NUMBER
            )
            if pr is None:
                return None

            memory_queries = ReviewMemoryQueryService()
            generations = await memory_queries.get_review_history_for_pr(session, pull_request_id=pr.id)
            open_findings = await memory_queries.get_open_memory_findings(session, pull_request_id=pr.id)
            transitions_by_generation: dict[uuid.UUID, int] = {}
            for generation in generations:
                transitions = await memory_queries.get_transitions_for_run(
                    session, review_run_id=generation.review_run_id
                )
                transitions_by_generation[generation.id] = len(transitions)

        return ReviewHistoryReport(
            generations=generations,
            open_findings=list(open_findings),
            transitions_by_generation=transitions_by_generation,
        )
    finally:
        await engine.dispose()


async def _publish_review_run(*, review_run_id: uuid.UUID, publish: bool) -> ReviewPublicationResult:
    """Publish (or dry-run plan) an already-completed review run to
    GitHub. Unlike ``index``/``analyze``/``context``/``review``, this
    subcommand operates on a *remote* pull request, not a local checkout
    -- the review run must already be associated with a real GitHub
    repository/pull request known to PatchFrog (i.e. one ingested through
    a real GitHub App installation, not a CLI-synthetic one; see
    :func:`_upsert_cli_repository`'s ``installation_id=0`` placeholder,
    which cannot obtain a real installation token)."""

    settings = get_settings()
    configure_logging(settings.log_level)

    engine = create_engine(settings.database_url)
    session_factory = create_session_factory(engine)
    try:
        async with session_factory() as session:
            run = await session.get(ReviewRunModel, review_run_id)
            if run is None:
                raise ReviewNotFoundError(f"no review run with id {review_run_id}")
            if run.pull_request_id is None:
                raise ReviewRunNotAssociatedWithPullRequestError(
                    f"review run {review_run_id} has no associated pull request"
                )
            repository = await session.get(RepositoryModel, run.repository_id)
            pull_request = await session.get(PullRequestModel, run.pull_request_id)
            assert repository is not None and pull_request is not None

        clone_url = f"https://github.com/{repository.full_name}.git"
        mode = ReviewPublicationMode.PUBLISH if publish else ReviewPublicationMode.DRY_RUN

        async with httpx.AsyncClient(timeout=settings.github_api_timeout_seconds) as http_client:
            token_provider = InstallationTokenProvider(
                http_client=http_client,
                app_id=settings.github_app_id,
                private_key=settings.github_private_key,
                api_base_url=settings.github_api_base_url,
            )
            token = await token_provider.get_token(repository.installation_id)

            config = await resolve_repository_publication_config(
                local=False,
                commit_sha=run.commit_sha,
                repository_full_name=repository.full_name,
                clone_url=clone_url,
                token=token,
            )

            github_client = GitHubClient(
                http_client=http_client,
                token_provider=token_provider,
                api_base_url=settings.github_api_base_url,
                timeout_seconds=settings.github_api_timeout_seconds,
            )
            publisher = GitHubClientReviewPublisher(
                github_client=github_client, installation_id=repository.installation_id
            )
            service = ReviewPublicationService(session_factory=session_factory, publisher=publisher)
            return await service.publish(review_run_id=review_run_id, mode=mode, config=config)
    finally:
        await engine.dispose()


def _run_index(args: argparse.Namespace) -> int:
    repository_path: Path = args.repository
    if not repository_path.is_dir():
        print(f"error: not a directory: {repository_path}", file=sys.stderr)
        return 1
    if not (repository_path / ".git").exists():
        print(f"error: not a git repository (no .git found): {repository_path}", file=sys.stderr)
        return 1

    full_name = args.full_name or _default_full_name(repository_path)
    try:
        summary = asyncio.run(_index_local(repository_path=repository_path, full_name=full_name))
    except GitError as exc:
        # Expected, user-actionable failures (dirty worktree, no commits
        # yet, git not usable, ...) — a clean one-line message, not a
        # Python traceback.
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(
        f"indexed {full_name}: files_total={summary.files_total} "
        f"files_parsed={summary.files_parsed} files_reused={summary.files_reused} "
        f"files_failed={summary.files_failed} symbols_extracted={summary.symbols_extracted} "
        f"edges_created={summary.edges_created} duration_ms={summary.duration_ms:.1f} "
        f"incremental={summary.incremental}"
    )
    return 0


def _run_analyze(args: argparse.Namespace) -> int:
    repository_path: Path = args.repository
    if not repository_path.is_dir():
        print(f"error: not a directory: {repository_path}", file=sys.stderr)
        return 1
    if not (repository_path / ".git").exists():
        print(f"error: not a git repository (no .git found): {repository_path}", file=sys.stderr)
        return 1

    full_name = args.full_name or _default_full_name(repository_path)
    try:
        summary = asyncio.run(_analyze_local(repository_path=repository_path, full_name=full_name))
    except GitError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except StaleIndexError as exc:
        print(f"error: {exc} — run 'index' for this commit first", file=sys.stderr)
        return 1

    print(
        f"analyzed {full_name}: status={summary.status.value} "
        f"analyzers_succeeded={summary.analyzers_succeeded} analyzers_failed={summary.analyzers_failed} "
        f"analyzers_skipped={summary.analyzers_skipped} raw_findings={summary.raw_findings_count} "
        f"findings={summary.findings_count} duration_ms={summary.duration_ms:.1f} "
        f"reused_existing_run={summary.reused_existing_run}"
    )
    return 0


def _run_context(args: argparse.Namespace) -> int:
    repository_path: Path = args.repository
    if not repository_path.is_dir():
        print(f"error: not a directory: {repository_path}", file=sys.stderr)
        return 1
    if not (repository_path / ".git").exists():
        print(f"error: not a git repository (no .git found): {repository_path}", file=sys.stderr)
        return 1
    if args.finding_id is None and (args.file is None or args.line is None):
        print("error: either --finding-id, or both --file and --line, are required", file=sys.stderr)
        return 1

    full_name = args.full_name or _default_full_name(repository_path)
    finding_id = uuid.UUID(args.finding_id) if args.finding_id else None
    try:
        bundle = asyncio.run(
            _context_local(
                repository_path=repository_path,
                full_name=full_name,
                finding_id=finding_id,
                file_path=args.file,
                line=args.line,
            )
        )
    except GitError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except StaleContextIndexError as exc:
        print(f"error: {exc} — run 'index' for this commit first", file=sys.stderr)
        return 1
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(
        f"context for {full_name}: target={bundle.target.target_type.value}:{bundle.target.file_path} "
        f"items={len(bundle.items)}/{bundle.metrics.candidate_count} tokens={bundle.total_tokens_estimate} "
        f"lines={bundle.total_lines} generation_ms={bundle.metrics.generation_ms:.1f} "
        f"reused={bundle.reused_existing_bundle}"
    )
    for rank, item in enumerate(bundle.items):
        print(
            f"  [{rank}] score={item.score:.3f} kind={item.kind.value} relationship={item.relationship.value} "
            f"file={item.file_path} lines={item.start_line}-{item.end_line} "
            f"tokens={item.estimated_tokens} truncated={item.truncated} reason={item.reason!r}"
        )
        if args.show_content:
            for content_line in item.content.splitlines():
                print(f"      | {content_line}")
    return 0


def _run_review(args: argparse.Namespace) -> int:
    repository_path: Path = args.repository
    if not repository_path.is_dir():
        print(f"error: not a directory: {repository_path}", file=sys.stderr)
        return 1
    if not (repository_path / ".git").exists():
        print(f"error: not a git repository (no .git found): {repository_path}", file=sys.stderr)
        return 1

    full_name = args.full_name or _default_full_name(repository_path)
    try:
        if args.dry_run:
            config, candidates, plan = asyncio.run(
                _review_dry_run(
                    repository_path=repository_path, full_name=full_name, base_ref=args.base,
                    incremental=args.incremental,
                )
            )
            print(
                f"dry-run for {full_name}: {len(candidates)} candidate(s) would be reviewed "
                f"(provider={config.provider} model={config.model}, no provider call made)"
            )
            for c in candidates:
                target = c.qualified_name or c.symbol_name or f"{c.file_path}:{c.start_line}"
                print(
                    f"  - {target} ({c.file_path}:{c.start_line}-{c.end_line}) reason={c.reason.value} "
                    f"changed_lines={len(c.changed_lines)} static_findings={len(c.static_finding_ids)}"
                )
            if plan is not None:
                m = plan.metrics
                print(
                    f"incremental plan: mode={plan.mode.value} "
                    f"ancestry_verified={plan.selection.ancestry_verified} "
                    f"usable_for_candidate_skipping={plan.selection.usable_for_candidate_skipping} "
                    f"usable_for_finding_memory={plan.selection.usable_for_finding_memory} "
                    f"detail={plan.selection.detail!r}"
                )
                print(
                    f"  candidates: previous={m.previous_candidate_count} full={m.current_full_candidate_count} "
                    f"selected={len(plan.selected_candidates)} skipped={len(plan.skipped_candidates)}"
                )
                print(
                    f"  memory: carried_forward={m.findings_carried_forward} resolved={m.findings_resolved} "
                    f"ambiguous={m.findings_ambiguous}"
                )
            return 0

        summary = asyncio.run(
            _review_local(
                repository_path=repository_path, full_name=full_name, base_ref=args.base,
                incremental=args.incremental,
            )
        )
    except GitError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except StaleReviewIndexError as exc:
        print(f"error: {exc} — run 'index' for this commit first", file=sys.stderr)
        return 1
    except MalformedReviewConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except MissingProviderCredentialsError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(
        f"reviewed {full_name}: status={summary.status.value} "
        f"candidates={summary.candidate_count} reviewed={summary.candidates_reviewed} "
        f"failed={summary.candidates_failed} skipped_budget={summary.candidates_skipped_budget} "
        f"accepted={summary.accepted_count} rejected={summary.rejected_count} "
        f"suppressed_duplicate={summary.suppressed_duplicate_count} "
        f"reviewer_tokens={summary.reviewer_usage.input_tokens}/{summary.reviewer_usage.output_tokens} "
        f"critic_tokens={summary.critic_usage.input_tokens}/{summary.critic_usage.output_tokens} "
        f"duration_ms={summary.duration_ms:.1f} reused={summary.reused_existing_run}"
    )
    return 0


def _run_review_history(args: argparse.Namespace) -> int:
    repository_path: Path = args.repository
    if not repository_path.is_dir():
        print(f"error: not a directory: {repository_path}", file=sys.stderr)
        return 1

    full_name = args.full_name or _default_full_name(repository_path)
    report = asyncio.run(_review_history(repository_path=repository_path, full_name=full_name))
    if report is None:
        print(f"no review-memory history for {full_name} -- run 'review --incremental' first")
        return 0

    print(f"review-memory history for {full_name}: {len(report.generations)} generation(s)")
    for generation in report.generations:
        transitions = report.transitions_by_generation.get(generation.id, 0)
        print(
            f"  - generation={generation.id} run={generation.review_run_id} "
            f"commit={generation.commit_sha[:12]} mode={generation.mode.value} "
            f"ancestry_verified={generation.ancestry_verified} compatibility_ok={generation.compatibility_ok} "
            f"invalidation_reason={generation.invalidation_reason.value if generation.invalidation_reason else None} "
            f"transitions={transitions}"
        )

    print(f"open memory findings: {len(report.open_findings)}")
    for finding in report.open_findings:
        print(
            f"  - {finding.file_path}:{finding.start_line}-{finding.end_line} "
            f"status={finding.status.value} severity={finding.severity.value} "
            f"title={finding.title!r}"
        )
    return 0


def _run_publish(args: argparse.Namespace) -> int:
    try:
        review_run_id = uuid.UUID(args.review_run_id)
    except ValueError:
        print(f"error: not a valid review run id: {args.review_run_id!r}", file=sys.stderr)
        return 1

    try:
        result = asyncio.run(_publish_review_run(review_run_id=review_run_id, publish=args.publish))
    except ReviewNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except ReviewRunNotAssociatedWithPullRequestError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    mode_label = "PUBLISH" if args.publish else "DRY-RUN"
    print(
        f"{mode_label} publish for review run {review_run_id}: status={result.status.value} "
        f"repository={result.repository_id} pr=#{result.pull_request_number} head_sha={result.head_sha} "
        f"planned_inline={result.planned_inline} published_inline={result.published_inline} "
        f"summary_only={result.summary_only} omitted={result.omitted} "
        f"github_review_id={result.github_review_id} reconciled={result.reconciled}"
    )
    if result.errors:
        for error in result.errors:
            print(f"  error: {error}", file=sys.stderr)
        return 1
    return 0


# ---------------------------------------------------------------------------
# Phase 8: quality evaluation harness (patchfrog.evaluation)
# ---------------------------------------------------------------------------

_ABLATION_KIND_SETS: dict[str, tuple[ContextItemKind, ...] | None] = {
    "normal": None,  # None -> production default (all relationship kinds)
    "target-only": (ContextItemKind.TARGET_SYMBOL, ContextItemKind.TARGET_FILE_REGION),
}


def _filter_cases(cases: list[EvaluationCase], args: argparse.Namespace) -> list[EvaluationCase]:
    from patchfrog.evaluation.domain import Difficulty, Language

    filtered = cases
    if args.case:
        wanted = set(args.case)
        filtered = [c for c in filtered if c.id in wanted]
    if args.tag:
        wanted_tags = set(args.tag)
        filtered = [c for c in filtered if wanted_tags & set(c.tags)]
    if args.language:
        filtered = [c for c in filtered if c.language is Language(args.language)]
    if args.difficulty:
        filtered = [c for c in filtered if c.difficulty is Difficulty(args.difficulty)]
    return filtered


def _build_provider_factory(
    args: argparse.Namespace, *, settings: Settings
) -> Callable[[EvaluationCase], LLMProvider]:
    if args.provider == "fake":
        return oracle_reviewer_provider_factory(cases_root=DEFAULT_CASES_ROOT)

    from patchfrog.review.config import ReviewConfig
    from patchfrog.review.provider_factory import build_reviewer_provider

    provider = build_reviewer_provider(ReviewConfig(), settings=settings)
    return lambda case: provider


async def _eval_run_async(args: argparse.Namespace) -> dict[str, Any]:
    settings = get_settings()
    configure_logging(settings.log_level)

    all_cases = load_all_cases(DEFAULT_CASES_ROOT)
    validate_and_raise(all_cases, cases_root=DEFAULT_CASES_ROOT)
    cases = _filter_cases(all_cases, args)
    if not cases:
        raise ValueError("no benchmark cases matched the given --case/--tag/--language/--difficulty filters")

    mode = EvaluationMode(args.mode)
    provider_factory = _build_provider_factory(args, settings=settings)

    engine = create_engine(settings.database_url)
    session_factory = create_session_factory(engine)
    try:
        runner = EvaluationRunner(session_factory=session_factory)
        context_override = None  # normal, production-equivalent context by default

        start = time.monotonic()
        critic_comparison_report: dict[str, Any] | None = None
        if args.critic == "both":
            results_off = await runner.run_suite(
                cases, cases_root=DEFAULT_CASES_ROOT, mode=mode, reviewer_provider_factory=provider_factory,
                critic_provider_factory=provider_factory, critic_enabled=False,
                context_config_override=context_override, timeout_seconds=args.timeout,
            )
            results_on = await runner.run_suite(
                cases, cases_root=DEFAULT_CASES_ROOT, mode=mode, reviewer_provider_factory=provider_factory,
                critic_provider_factory=provider_factory, critic_enabled=True,
                context_config_override=context_override, timeout_seconds=args.timeout,
            )
            case_results = results_on
            critic_enabled_flag = True
            comparison = compute_critic_comparison(results_off, results_on)
            critic_comparison_report = {
                "critic_off": _asdict_metrics(comparison.critic_off),
                "critic_on": _asdict_metrics(comparison.critic_on),
                "precision_delta": comparison.precision_delta,
                "recall_delta": comparison.recall_delta,
                "false_positive_delta": comparison.false_positive_delta,
                "unsupported_delta": comparison.unsupported_delta,
            }
        else:
            critic_enabled_flag = args.critic != "off"
            case_results = await runner.run_suite(
                cases, cases_root=DEFAULT_CASES_ROOT, mode=mode, reviewer_provider_factory=provider_factory,
                critic_provider_factory=provider_factory, critic_enabled=critic_enabled_flag,
                context_config_override=context_override, timeout_seconds=args.timeout,
            )

        context_ablation_report: dict[str, Any] | None = None
        if args.context_ablation:
            context_ablation_report = await _run_context_ablation(
                runner, cases, mode=mode, provider_factory=provider_factory, timeout=args.timeout,
            )

        incremental_scenarios: tuple[IncrementalScenarioResult, ...] = ()
        if args.incremental_scenarios:
            import tempfile

            with tempfile.TemporaryDirectory() as tmp:
                incremental_scenarios = tuple(
                    await run_all_incremental_scenarios(session_factory, tmp_path_root=Path(tmp))
                )

        duration_ms = (time.monotonic() - start) * 1000

        identity = build_evaluation_identity(
            mode=mode, reviewer_provider=provider_factory(cases[0]), critic_enabled=critic_enabled_flag,
            cases=cases, cases_root=DEFAULT_CASES_ROOT,
        )
        cases_by_id = {c.id: c for c in cases}
        fixture_info = {c.id: fixture_info_for_case(c, cases_root=DEFAULT_CASES_ROOT) for c in cases}

        result = EvaluationRunResult(
            identity=identity, generated_at=datetime.now(UTC).isoformat(), duration_ms=duration_ms,
            case_results=tuple(case_results), incremental_scenarios=incremental_scenarios,
        )
        report = build_report(result, cases_by_id=cases_by_id, fixture_info=fixture_info)
        report["benchmark_label"] = "pipeline_correctness" if args.provider == "fake" else "ai_quality"
        report["ai_quality_measured"] = args.provider == "live"
        if critic_comparison_report is not None:
            report["critic_comparison"] = critic_comparison_report
        if context_ablation_report is not None:
            report["context_ablation"] = context_ablation_report
        return report
    finally:
        await engine.dispose()


def _asdict_metrics(metrics: Any) -> dict[str, Any]:
    from dataclasses import asdict

    return asdict(metrics)


async def _run_context_ablation(
    runner: EvaluationRunner,
    cases: list[EvaluationCase],
    *,
    mode: EvaluationMode,
    provider_factory: Callable[[EvaluationCase], LLMProvider],
    timeout: float,
) -> dict[str, Any]:
    from patchfrog.evaluation.metrics import compute_overall_metrics

    variants: dict[str, Any] = {}
    for label, kinds in _ABLATION_KIND_SETS.items():
        override = ContextConfig(enabled_kinds=kinds) if kinds is not None else None
        results = await runner.run_suite(
            cases, cases_root=DEFAULT_CASES_ROOT, mode=mode, reviewer_provider_factory=provider_factory,
            critic_provider_factory=provider_factory, critic_enabled=True, context_config_override=override,
            timeout_seconds=timeout,
        )
        variants[label] = _asdict_metrics(compute_overall_metrics(results))
    return variants


def _print_eval_summary(report: dict[str, Any]) -> None:
    m = report["metrics"]["overall"]
    print(
        f"eval run: cases={m['cases']} (errors={m['error_cases']}) "
        f"TP={m['confusion']['true_positives']} FP={m['confusion']['false_positives']} "
        f"missed={m['confusion']['missed']} precision={m['scores']['precision']:.3f} "
        f"recall={m['scores']['recall']:.3f} f1={m['scores']['f1']:.3f} "
        f"[{report['benchmark_label']}]"
    )
    clean = report["metrics"]["clean"]
    print(
        f"  clean-case pass rate: {clean['pass_rate']:.3f} "
        f"({clean['clean_cases_passed']}/{clean['clean_cases']})"
    )


def _run_eval_run(args: argparse.Namespace) -> int:
    try:
        report = asyncio.run(_eval_run_async(args))
    except EvalBenchmarkValidationError as exc:
        print(f"error: benchmark ground truth invalid:\n{exc}", file=sys.stderr)
        return 1
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    output_path = Path(args.output) if args.output else DEFAULT_BASELINES_ROOT / "latest_run.json"
    write_json(report, output_path)
    print(f"wrote JSON report to {output_path}")
    if args.markdown_output:
        Path(args.markdown_output).parent.mkdir(parents=True, exist_ok=True)
        Path(args.markdown_output).write_text(render_markdown(report))
        print(f"wrote Markdown report to {args.markdown_output}")

    if args.json:
        import json as _json

        print(_json.dumps(report, indent=2, sort_keys=True))
    else:
        _print_eval_summary(report)

    timed_out = sum(1 for r in report["case_results"] if r["status"] == CaseStatus.TIMEOUT.value)
    infra_errors = sum(1 for r in report["case_results"] if r["status"] == CaseStatus.INFRASTRUCTURE_ERROR.value)
    if timed_out or infra_errors:
        print(f"  warning: {timed_out} case(s) timed out, {infra_errors} infrastructure error(s)", file=sys.stderr)
    return 0


def _run_eval_compare(args: argparse.Namespace) -> int:
    baseline_path = Path(args.baseline) if args.baseline else default_baseline_path()
    current_path = Path(args.current) if args.current else DEFAULT_BASELINES_ROOT / "latest_run.json"
    try:
        baseline = read_json(baseline_path)
        current = read_json(current_path)
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    thresholds = RegressionThresholds(
        max_precision_drop=args.max_precision_drop, max_clean_pass_rate_drop=args.max_clean_pass_rate_drop,
        max_recall_drop=args.max_recall_drop,
    )
    verdict = compare_evaluation_runs(baseline, current, thresholds=thresholds)

    if not verdict.identity_compatible:
        print(f"error: baseline and current run identities are not comparable: {verdict.identity_mismatch_detail}", file=sys.stderr)
        return verdict.exit_code

    for check in verdict.checks:
        status = "PASS" if check.passed else "FAIL"
        print(f"[{status}] {check.name}: {check.detail}")
    print(f"overall: {'PASS' if verdict.passed else 'REGRESSION'} (exit_code={verdict.exit_code})")
    return verdict.exit_code


def _run_eval_report(args: argparse.Namespace) -> int:
    input_path = Path(args.input) if args.input else DEFAULT_BASELINES_ROOT / "latest_run.json"
    try:
        report = read_json(input_path)
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    markdown = render_markdown(report)
    if args.output:
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        Path(args.output).write_text(markdown)
        print(f"wrote {args.output}")
    else:
        print(markdown)
    return 0


def _run_eval_update_baseline(args: argparse.Namespace) -> int:
    if not args.confirm:
        print("error: refusing to overwrite the golden baseline without --confirm", file=sys.stderr)
        return 1
    input_path = Path(args.input) if args.input else DEFAULT_BASELINES_ROOT / "latest_run.json"
    baseline_path = Path(args.baseline) if args.baseline else default_baseline_path()
    try:
        report = read_json(input_path)
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    write_json(report, baseline_path)
    print(f"updated golden baseline: {baseline_path}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="patchfrog.cli")
    subparsers = parser.add_subparsers(dest="command", required=True)

    index_parser = subparsers.add_parser("index", help="Index a local repository checkout")
    index_parser.add_argument(
        "--repository", required=True, type=Path, help="Path to a local git checkout"
    )
    index_parser.add_argument(
        "--full-name",
        default=None,
        help="Repository identity, e.g. 'owner/repo' (defaults to the directory name)",
    )

    analyze_parser = subparsers.add_parser(
        "analyze", help="Run static analysis on a local repository checkout (requires 'index' first)"
    )
    analyze_parser.add_argument(
        "--repository", required=True, type=Path, help="Path to a local git checkout"
    )
    analyze_parser.add_argument(
        "--full-name",
        default=None,
        help="Repository identity, e.g. 'owner/repo' (defaults to the directory name)",
    )

    context_parser = subparsers.add_parser(
        "context", help="Build a deterministic context bundle for a finding or file/line (requires 'index' first)"
    )
    context_parser.add_argument(
        "--repository", required=True, type=Path, help="Path to a local git checkout"
    )
    context_parser.add_argument(
        "--full-name",
        default=None,
        help="Repository identity, e.g. 'owner/repo' (defaults to the directory name)",
    )
    context_parser.add_argument("--finding-id", default=None, help="Build context for this Finding's id")
    context_parser.add_argument("--file", default=None, help="Repo-relative file path (with --line)")
    context_parser.add_argument("--line", default=None, type=int, help="1-indexed line number (with --file)")
    context_parser.add_argument(
        "--show-content", action="store_true", help="Also print each item's extracted source content"
    )

    review_parser = subparsers.add_parser(
        "review",
        help=(
            "Run the AI reviewer against the diff since --base (requires 'index' first; "
            "--dry-run never calls the LLM provider)"
        ),
    )
    review_parser.add_argument(
        "--repository", required=True, type=Path, help="Path to a local git checkout"
    )
    review_parser.add_argument(
        "--full-name",
        default=None,
        help="Repository identity, e.g. 'owner/repo' (defaults to the directory name)",
    )
    review_parser.add_argument(
        "--base", default="HEAD~1", help="Base ref to diff against (default: HEAD~1)"
    )
    review_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Build candidates and context only -- never constructs a provider or calls the LLM",
    )
    review_parser.add_argument(
        "--incremental",
        action="store_true",
        help=(
            "Enable Phase 7 incremental review memory, scoped to a synthetic local PR tied to "
            "this repository checkout. With --dry-run, also prints the incremental plan (no "
            "provider call, no memory persisted)."
        ),
    )

    history_parser = subparsers.add_parser(
        "review-history",
        help="Inspect Phase 7 review-generation and finding-memory history for a local checkout's synthetic PR",
    )
    history_parser.add_argument(
        "--repository", required=True, type=Path, help="Path to a local git checkout"
    )
    history_parser.add_argument(
        "--full-name",
        default=None,
        help="Repository identity, e.g. 'owner/repo' (defaults to the directory name)",
    )

    publish_parser = subparsers.add_parser(
        "publish",
        help=(
            "Plan (default) or actually publish an already-completed review run's findings "
            "as a GitHub Pull Request Review. Safe by default: no GitHub write happens "
            "without --publish."
        ),
    )
    publish_parser.add_argument(
        "--review-run-id", required=True, help="id of an already-completed patchfrog.review_runs row"
    )
    publish_parser.add_argument(
        "--publish",
        action="store_true",
        help="Actually write the review to GitHub. Without this flag, only plans and reports (no write).",
    )

    eval_parser = subparsers.add_parser("eval", help="Phase 8 quality evaluation harness (patchfrog.evaluation)")
    eval_subparsers = eval_parser.add_subparsers(dest="eval_command", required=True)

    eval_run_parser = eval_subparsers.add_parser(
        "run", help="Run the benchmark corpus (or a filtered subset) and write a JSON/Markdown report"
    )
    eval_run_parser.add_argument(
        "--mode", choices=[m.value for m in EvaluationMode], default=EvaluationMode.FULL_PIPELINE.value,
    )
    eval_run_parser.add_argument("--case", action="append", default=[], help="Run only this case id (repeatable)")
    eval_run_parser.add_argument("--tag", action="append", default=[], help="Run only cases with this tag (repeatable)")
    eval_run_parser.add_argument("--language", default=None, help="Run only cases in this language")
    eval_run_parser.add_argument("--difficulty", default=None, help="Run only cases at this difficulty")
    eval_run_parser.add_argument(
        "--critic", choices=["on", "off", "both"], default="on",
        help="'both' additionally runs critic-off and reports the precision/recall delta",
    )
    eval_run_parser.add_argument(
        "--provider", choices=["fake", "live"], default="fake",
        help="'fake' scripts a deterministic oracle from each case's own ground truth (pipeline-correctness "
        "benchmark, never AI quality). 'live' calls the real configured provider and requires "
        "ANTHROPIC_API_KEY.",
    )
    eval_run_parser.add_argument(
        "--context-ablation", action="store_true",
        help="Additionally run normal vs. target-only context and report the quality delta",
    )
    eval_run_parser.add_argument(
        "--incremental-scenarios", action="store_true", help="Additionally run the Phase 7 incremental benchmark scenarios"
    )
    eval_run_parser.add_argument("--timeout", type=float, default=120.0, help="Per-case timeout in seconds")
    eval_run_parser.add_argument("--output", default=None, help="JSON report path (default: evaluation_baselines/latest_run.json)")
    eval_run_parser.add_argument("--markdown-output", default=None, help="Also write a Markdown report to this path")
    eval_run_parser.add_argument("--json", action="store_true", help="Print the full JSON report to stdout")

    eval_compare_parser = eval_subparsers.add_parser(
        "compare", help="Compare a run's JSON report against the golden baseline and apply regression thresholds"
    )
    eval_compare_parser.add_argument("--baseline", default=None, help="Baseline JSON path (default: evaluation_baselines/phase8_baseline.json)")
    eval_compare_parser.add_argument("--current", default=None, help="Current run JSON path (default: evaluation_baselines/latest_run.json)")
    eval_compare_parser.add_argument("--max-precision-drop", type=float, default=RegressionThresholds().max_precision_drop)
    eval_compare_parser.add_argument("--max-clean-pass-rate-drop", type=float, default=RegressionThresholds().max_clean_pass_rate_drop)
    eval_compare_parser.add_argument("--max-recall-drop", type=float, default=RegressionThresholds().max_recall_drop)

    eval_report_parser = eval_subparsers.add_parser("report", help="Render a JSON report as Markdown")
    eval_report_parser.add_argument("--input", default=None, help="JSON report path (default: evaluation_baselines/latest_run.json)")
    eval_report_parser.add_argument("--output", default=None, help="Write Markdown here instead of stdout")

    eval_update_baseline_parser = eval_subparsers.add_parser(
        "update-baseline", help="Explicitly promote a run's JSON report to the golden baseline"
    )
    eval_update_baseline_parser.add_argument("--confirm", action="store_true", help="Required -- refuses to overwrite the baseline otherwise")
    eval_update_baseline_parser.add_argument("--input", default=None, help="JSON report path (default: evaluation_baselines/latest_run.json)")
    eval_update_baseline_parser.add_argument("--baseline", default=None, help="Baseline path (default: evaluation_baselines/phase8_baseline.json)")

    args = parser.parse_args(argv)
    if args.command == "index":
        return _run_index(args)
    if args.command == "analyze":
        return _run_analyze(args)
    if args.command == "context":
        return _run_context(args)
    if args.command == "review":
        return _run_review(args)
    if args.command == "review-history":
        return _run_review_history(args)
    if args.command == "publish":
        return _run_publish(args)
    if args.command == "eval":
        if args.eval_command == "run":
            return _run_eval_run(args)
        if args.eval_command == "compare":
            return _run_eval_compare(args)
        if args.eval_command == "report":
            return _run_eval_report(args)
        if args.eval_command == "update-baseline":
            return _run_eval_update_baseline(args)

    return 1


if __name__ == "__main__":
    sys.exit(main())
