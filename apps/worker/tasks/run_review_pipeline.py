"""Celery task: the index -> analyze -> review pipeline orchestrator.

Runs after :mod:`patchfrog.ops.orchestrator` has already decided a pull
request is eligible (installation active, repository selected, quota and
kill switches all clear -- see :mod:`patchfrog.ops.eligibility`). This
task owns exactly two things neither existing stage owned before:

1. The one resource-limit check (max changed files / max diff bytes,
   spec section 22) that must happen *before* indexing, not after --
   fetched once here via a single extra GitHub API call, never inside
   :mod:`apps.worker.tasks.index_repository`/``analyze_repository``
   themselves (neither is redesigned).
2. Sequencing ``_index`` then ``_analyze`` (reusing their existing async
   helpers directly, not re-implementing indexing/analysis) and, on
   success, enqueueing the existing
   :mod:`apps.worker.tasks.review_pull_request` task for the expensive
   AI stage -- kept as its own separately-retriable task, exactly as
   before.

Indexing failure is fatal to this run (nothing downstream can proceed
without an index). Analysis failure is *not* fatal -- static findings are
optional hints to the AI reviewer (see the module docstring of
:mod:`patchfrog.review.prompt`); a broken/unavailable analyzer must never
block the AI review stage, only leave it without static hints.
"""

from __future__ import annotations

import asyncio

import httpx
import structlog

from apps.worker.celery_app import celery_app
from apps.worker.tasks.analyze_repository import _analyze
from apps.worker.tasks.index_repository import _index
from apps.worker.tasks.review_pull_request import review_pull_request_task
from patchfrog.config.settings import Settings, get_settings
from patchfrog.domain.pull_request import PullRequestRef
from patchfrog.github.auth import InstallationTokenProvider
from patchfrog.github.client import GitHubClient
from patchfrog.ops import metrics
from patchfrog.ops.eligibility import check_resource_limits
from patchfrog.ops.errors import classify_exception

logger = structlog.get_logger(__name__)


class RetryablePipelineError(RuntimeError):
    """Raised to signal Celery's ``autoretry_for`` machinery -- mirrors
    :class:`apps.worker.tasks.publish_review.RetryablePublicationError`
    exactly: :func:`patchfrog.ops.errors.classify_exception` decides
    retryability per-exception at runtime, which a static
    ``autoretry_for`` class tuple can't express directly, so a retryable
    classification is re-raised as this one dedicated type instead."""


async def _run_pipeline(
    *,
    github_repository_id: int,
    owner: str,
    name: str,
    full_name: str,
    installation_id: int,
    commit_sha: str,
    pull_request_number: int,
    settings: Settings,
) -> str:
    ref = PullRequestRef(owner=owner, repository=name, number=pull_request_number)

    try:
        async with httpx.AsyncClient(timeout=settings.github_api_timeout_seconds) as http_client:
            token_provider = InstallationTokenProvider(
                http_client=http_client,
                app_id=settings.github_app_id,
                private_key=settings.github_private_key,
                api_base_url=settings.github_api_base_url,
            )
            github_client = GitHubClient(
                http_client=http_client,
                token_provider=token_provider,
                api_base_url=settings.github_api_base_url,
                timeout_seconds=settings.github_api_timeout_seconds,
            )
            changed_files = await github_client.list_pull_request_files(
                installation_id=installation_id, ref=ref
            )
    except Exception as exc:
        category, retryable, detail = classify_exception(exc)
        logger.error(
            "pull_request_changed_files_fetch_failed",
            repository=full_name,
            pull_request_number=pull_request_number,
            commit_sha=commit_sha,
            error_category=category.value,
            retryable=retryable,
            detail=detail,
        )
        if retryable:
            raise RetryablePipelineError(f"{category.value}: {detail}") from exc
        raise

    diff_bytes = sum(len(f.patch or "") for f in changed_files)
    limits_ok, limits_detail = check_resource_limits(
        settings=settings, changed_files_count=len(changed_files), diff_bytes=diff_bytes
    )
    if not limits_ok:
        logger.info(
            "pull_request_resource_limit_exceeded",
            repository=full_name,
            pull_request_number=pull_request_number,
            commit_sha=commit_sha,
            detail=limits_detail,
        )
        return f"skipped: {limits_detail}"

    try:
        index_summary = await _index(
            github_repository_id=github_repository_id,
            owner=owner,
            name=name,
            full_name=full_name,
            installation_id=installation_id,
            commit_sha=commit_sha,
            settings=settings,
        )
        metrics.repository_index_duration_seconds.observe(index_summary.duration_ms / 1000)
    except Exception as exc:
        category, retryable, detail = classify_exception(exc)
        logger.error(
            "pull_request_indexing_failed",
            repository=full_name,
            pull_request_number=pull_request_number,
            commit_sha=commit_sha,
            error_category=category.value,
            retryable=retryable,
            detail=detail,
        )
        if retryable:
            raise RetryablePipelineError(f"{category.value}: {detail}") from exc
        raise

    try:
        analyze_summary = await _analyze(
            github_repository_id=github_repository_id,
            owner=owner,
            name=name,
            full_name=full_name,
            installation_id=installation_id,
            commit_sha=commit_sha,
            settings=settings,
        )
        metrics.static_analysis_duration_seconds.observe(analyze_summary.duration_ms / 1000)
    except Exception as exc:
        category, retryable, detail = classify_exception(exc)
        logger.warning(
            "pull_request_static_analysis_failed_continuing",
            repository=full_name,
            pull_request_number=pull_request_number,
            commit_sha=commit_sha,
            error_category=category.value,
            retryable=retryable,
            detail=detail,
        )

    review_pull_request_task.delay(
        github_repository_id=github_repository_id,
        owner=owner,
        name=name,
        full_name=full_name,
        installation_id=installation_id,
        pull_request_number=pull_request_number,
        head_sha=commit_sha,
    )
    return "scheduled"


@celery_app.task(  # type: ignore[untyped-decorator]
    name="patchfrog.run_review_pipeline",
    autoretry_for=(RetryablePipelineError,),
    max_retries=3,
    retry_backoff=15,
    retry_backoff_max=300,
    retry_jitter=True,
)
def run_review_pipeline_task(
    *,
    github_repository_id: int,
    owner: str,
    name: str,
    full_name: str,
    installation_id: int,
    commit_sha: str,
    pull_request_number: int,
) -> str:
    result = asyncio.run(
        _run_pipeline(
            github_repository_id=github_repository_id,
            owner=owner,
            name=name,
            full_name=full_name,
            installation_id=installation_id,
            commit_sha=commit_sha,
            pull_request_number=pull_request_number,
            settings=get_settings(),
        )
    )
    logger.info(
        "run_review_pipeline_task_completed",
        repository=full_name,
        pull_request_number=pull_request_number,
        commit_sha=commit_sha,
        result=result,
    )
    return result
