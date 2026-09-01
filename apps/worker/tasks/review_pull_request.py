"""Celery task: run the AI reviewer against one pull request.

Thin adapter only — resolves the installation access token, re-fetches
the PR's changed files from GitHub (diff hunks are not persisted by
ingestion; see :mod:`patchfrog.services.pull_request_ingestion`), resolves
the repository's *exact-commit* ``.patchfrog.yml`` review config (see
:mod:`patchfrog.review.config_resolution` -- the same function the CLI
uses, so the two paths can never diverge in what config a given
repository/commit resolves to), independently resolves the operator-
controlled AI provider runtime config (see
:mod:`patchfrog.review.runtime_config` -- again the same function the
CLI uses, so provider/model selection can never diverge between the two
either), and delegates all real work to
:class:`patchfrog.review.service.PullRequestReviewService`. Phase 2's
repository index for the exact commit under review must already exist
(see :class:`patchfrog.review.service.StaleReviewIndexError`) -- the same
"analysis/context/review commit SHA == repository index commit SHA"
invariant Phases 3 and 4 established.

Never posts a GitHub comment, never applies a suggested fix, never
commits anything -- this task's only side effect is persisting rows via
:class:`~patchfrog.review.service.PullRequestReviewService`. Posting
review results to a PR is an explicit Phase 5 non-goal; see the Phase 5
PR description.
"""

from __future__ import annotations

import asyncio
import uuid

import httpx
import structlog

from apps.worker.celery_app import celery_app
from apps.worker.tasks.publish_review import publish_review_task
from patchfrog.config.settings import Settings, get_settings
from patchfrog.diff.parser import build_diff_file
from patchfrog.domain.pull_request import PullRequestRef
from patchfrog.github.auth import InstallationTokenProvider
from patchfrog.github.client import GitHubClient
from patchfrog.ops import metrics
from patchfrog.ops.errors import classify_exception
from patchfrog.persistence.database import create_engine, create_session_factory
from patchfrog.persistence.repositories import (
    InstallationRepository,
    PullRequestRepository,
    RepositoryRepository,
)
from patchfrog.review.config import MalformedReviewConfigError
from patchfrog.review.config_resolution import (
    apply_operator_hard_caps,
    resolve_repository_review_config,
)
from patchfrog.review.domain import ReviewRunStatus, ReviewRunSummary
from patchfrog.review.provider_factory import build_critic_provider, build_reviewer_provider
from patchfrog.review.runtime_config import resolve_review_runtime_config
from patchfrog.review.service import (
    PullRequestReviewService,
    persist_malformed_config_failure,
    require_matching_review_index,
)
from patchfrog.review_memory.config_resolution import resolve_repository_incremental_config
from patchfrog.review_memory.service import IncrementalReviewMemoryService

logger = structlog.get_logger(__name__)


async def _review_pull_request(
    *,
    github_repository_id: int,
    owner: str,
    name: str,
    full_name: str,
    installation_id: int,
    pull_request_number: int,
    head_sha: str,
    settings: Settings,
) -> ReviewRunSummary | None:
    """Returns ``None`` -- never a synthetic/placeholder
    :class:`~patchfrog.review.domain.ReviewRunSummary` -- when this
    commit was superseded by a newer one before the AI review stage
    started (spec section 21: "verify review head is still relevant"
    before spending any provider call on it). No review run row is
    created for a superseded commit; the outcome is observable only via
    the ``review_skipped_superseded`` structured log line and the
    ``reviews_skipped_total`` metric, deliberately not a DB row -- Phase
    5's own run-identity/locking machinery is untouched by this check."""

    engine = create_engine(settings.database_url)
    session_factory = create_session_factory(engine)

    try:
        async with session_factory() as session:
            repository_row = await RepositoryRepository().upsert(
                session,
                github_repository_id=github_repository_id,
                owner=owner,
                name=name,
                full_name=full_name,
                installation_id=installation_id,
            )
            await session.commit()
            repository_id = repository_row.id

            pull_request_row = await PullRequestRepository().get_by_repository_and_number(
                session, repository_id=repository_id, github_pr_number=pull_request_number
            )
            pull_request_id: uuid.UUID | None = pull_request_row.id if pull_request_row is not None else None

        async with httpx.AsyncClient(timeout=settings.github_api_timeout_seconds) as http_client:
            token_provider = InstallationTokenProvider(
                http_client=http_client,
                app_id=settings.github_app_id,
                private_key=settings.github_private_key,
                api_base_url=settings.github_api_base_url,
            )
            token = await token_provider.get_token(installation_id)

            github_client = GitHubClient(
                http_client=http_client,
                token_provider=token_provider,
                api_base_url=settings.github_api_base_url,
                timeout_seconds=settings.github_api_timeout_seconds,
            )

            ref = PullRequestRef(owner=owner, repository=name, number=pull_request_number)
            current_metadata = await github_client.get_pull_request(installation_id=installation_id, ref=ref)
            if current_metadata.head_sha != head_sha:
                logger.info(
                    "review_skipped_superseded",
                    repository=full_name,
                    pull_request_number=pull_request_number,
                    queued_head_sha=head_sha,
                    current_head_sha=current_metadata.head_sha,
                )
                metrics.reviews_skipped_total.labels(reason="superseded").inc()
                return None

            metrics.reviews_started_total.inc()
            changed_files = await github_client.list_pull_request_files(
                installation_id=installation_id, ref=ref
            )

        diff_files = [build_diff_file(f.path, f.patch) for f in changed_files]

        clone_url = f"https://github.com/{full_name}.git"
        try:
            review_config = await resolve_repository_review_config(
                local=False,
                commit_sha=head_sha,
                repository_full_name=full_name,
                clone_url=clone_url,
                token=token,
            )
            review_config = apply_operator_hard_caps(review_config, settings=settings)
        except MalformedReviewConfigError as exc:
            await persist_malformed_config_failure(
                session_factory,
                repository_id=repository_id,
                commit_sha=head_sha,
                pull_request_id=pull_request_id,
                exc=exc,
            )
            raise

        # Provider/model selection is operator/deployment-controlled --
        # resolved from trusted Settings, never from the repository's
        # review_config above (a PR changing .patchfrog.yml must never
        # change which provider/model actually runs).
        runtime_config = resolve_review_runtime_config(settings)
        reviewer_provider = build_reviewer_provider(runtime_config, settings=settings)
        critic_provider = build_critic_provider(
            runtime_config, settings=settings, critic_enabled=review_config.critic_enabled
        )

        incremental_config = await resolve_repository_incremental_config(
            local=False,
            commit_sha=head_sha,
            repository_full_name=full_name,
            clone_url=clone_url,
            token=token,
        )

        memory_service = IncrementalReviewMemoryService(session_factory=session_factory)
        repository_index_id = await require_matching_review_index(
            session_factory, repository_id=repository_id, commit_sha=head_sha
        )
        full_candidates = await memory_service.build_candidates(
            repository_id=repository_id,
            repository_index_id=repository_index_id,
            commit_sha=head_sha,
            diff_files=diff_files,
            max_candidates=review_config.max_candidates,
        )
        prepared = await memory_service.prepare(
            pull_request_id=pull_request_id,
            repository_index_id=repository_index_id,
            commit_sha=head_sha,
            clone_url=clone_url,
            token=token,
            current_candidates=full_candidates,
            reviewer_provider=reviewer_provider.identity.provider,
            reviewer_model=reviewer_provider.identity.model,
            incremental_config=incremental_config,
        )

        service = PullRequestReviewService(
            session_factory=session_factory,
            reviewer_provider=reviewer_provider,
            critic_provider=critic_provider,
        )
        summary = await service.review_pull_request(
            repository_id=repository_id,
            clone_url=clone_url,
            commit_sha=head_sha,
            repository_full_name=full_name,
            token=token,
            diff_files=diff_files,
            pull_request_id=pull_request_id,
            config=review_config,
            candidate_filter=prepared.candidate_filter,
            incremental_context_fingerprint=prepared.incremental_context_fingerprint,
        )

        if prepared.memory_tracking_active and pull_request_id is not None:
            await memory_service.finalize(
                review_run_id=summary.run_id,
                repository_id=repository_id,
                pull_request_id=pull_request_id,
                commit_sha=head_sha,
                prepared=prepared,
            )

        provider_labels = {
            "provider": reviewer_provider.identity.provider,
            "model": reviewer_provider.identity.model,
        }
        metrics.reviews_completed_total.labels(status=summary.status.value).inc()
        metrics.review_duration_seconds.observe(summary.duration_ms / 1000)
        metrics.provider_calls_total.labels(**provider_labels, role="reviewer").inc(summary.candidates_reviewed)
        metrics.provider_input_tokens_total.labels(**provider_labels).inc(summary.reviewer_usage.input_tokens)
        metrics.provider_output_tokens_total.labels(**provider_labels).inc(summary.reviewer_usage.output_tokens)
        metrics.findings_generated_total.inc(summary.proposals_count)
        metrics.findings_suppressed_total.labels(reason="duplicate").inc(summary.suppressed_duplicate_count)
        for tier, count in summary.candidates_by_tier.items():
            metrics.candidates_by_tier_total.labels(tier=tier.value).inc(count)
        metrics.candidates_skipped_budget_total.inc(summary.candidates_skipped_budget)
        metrics.critic_calls_total.inc(summary.critic_calls)

        return summary
    finally:
        await engine.dispose()


@celery_app.task(name="patchfrog.review_pull_request")  # type: ignore[untyped-decorator]
def review_pull_request_task(
    *,
    github_repository_id: int,
    owner: str,
    name: str,
    full_name: str,
    installation_id: int,
    pull_request_number: int,
    head_sha: str,
) -> str:
    settings = get_settings()
    try:
        summary = asyncio.run(
            _review_pull_request(
                github_repository_id=github_repository_id,
                owner=owner,
                name=name,
                full_name=full_name,
                installation_id=installation_id,
                pull_request_number=pull_request_number,
                head_sha=head_sha,
                settings=settings,
            )
        )
    except Exception as exc:
        category, _retryable, _detail = classify_exception(exc)
        metrics.reviews_failed_total.labels(error_category=category.value).inc()
        raise
    if summary is None:
        return "skipped: superseded by a newer commit"

    logger.info(
        "review_task_completed",
        repository=full_name,
        pull_request_number=pull_request_number,
        status=summary.status.value,
        accepted_count=summary.accepted_count,
        reused_existing_run=summary.reused_existing_run,
    )

    if summary.status is not ReviewRunStatus.FAILED:
        if asyncio.run(_publication_allowed(installation_id=installation_id, settings=settings)):
            publish_review_task.delay(review_run_id=str(summary.run_id), publish=True)
            logger.info("publish_scheduled", review_run_id=str(summary.run_id), repository=full_name)
        else:
            logger.info(
                "publish_not_scheduled_beta_gate_closed",
                review_run_id=str(summary.run_id),
                repository=full_name,
            )

    return (
        f"status={summary.status.value} accepted={summary.accepted_count} "
        f"rejected={summary.rejected_count} reviewed={summary.candidates_reviewed}"
    )


async def _publication_allowed(*, installation_id: int, settings: Settings) -> bool:
    """The beta-specific publication gate (spec sections 11/35): *both*
    the process-wide kill switch and this specific installation's own
    opt-in must be true. A repository's own ``.patchfrog.yml``
    ``publish.enabled`` is checked separately, inside
    :mod:`apps.worker.tasks.publish_review` itself -- neither gate alone
    is sufficient for a real GitHub write to happen."""

    if not settings.global_publication_enabled:
        return False

    engine = create_engine(settings.database_url)
    session_factory = create_session_factory(engine)
    try:
        async with session_factory() as session:
            installation = await InstallationRepository().get_by_github_id(
                session, github_installation_id=installation_id
            )
            return installation is not None and installation.publication_allowed
    finally:
        await engine.dispose()
