"""Celery task: publish a completed AI review run to GitHub as a Pull
Request Review.

Thin adapter only, exactly mirroring
:mod:`apps.worker.tasks.review_pull_request`'s shape -- resolves the
installation access token, resolves the repository's exact-commit
``.patchfrog.yml`` ``publish:`` config (see
:mod:`patchfrog.publishing.config_resolution`, the same code path the CLI
uses), and delegates all real work to
:class:`patchfrog.publishing.service.ReviewPublicationService`.

Deliberately a *separate* task from ``patchfrog.review_pull_request`` --
review generation (an AI operation, potentially slow/expensive/flaky) and
publication (a deterministic, retryable side effect) have different retry
semantics and must be retriable independently (see the module docstring
of :mod:`patchfrog.publishing.service`). Nothing here is wired to run
automatically after ``patchfrog.review_pull_request`` completes; invoking
this task is always a separate, explicit action.

Retry policy: only :class:`~patchfrog.publishing.errors.PublicationFailureClass.RETRYABLE`
and ``RATE_LIMITED`` failures are retried, honoring GitHub's
``Retry-After`` when present for rate limits. Stale-head, diff-mapping,
and permission failures are never retried -- retrying them cannot change
the outcome (see :mod:`patchfrog.publishing.errors`).
"""

from __future__ import annotations

import asyncio
import uuid

import httpx
import structlog
from celery.exceptions import Reject

from apps.worker.celery_app import celery_app
from patchfrog.config.settings import Settings, get_settings
from patchfrog.github.auth import InstallationTokenProvider
from patchfrog.github.client import GitHubClient
from patchfrog.persistence.database import create_engine, create_session_factory
from patchfrog.persistence.models.pull_request import PullRequestModel
from patchfrog.persistence.models.repository import RepositoryModel
from patchfrog.persistence.models.review import ReviewRunModel
from patchfrog.persistence.repositories.review_memory_finding import ReviewMemoryFindingRepository
from patchfrog.publishing.config_resolution import resolve_repository_publication_config
from patchfrog.publishing.domain import (
    ReviewPublicationMode,
    ReviewPublicationResult,
    ReviewPublicationStatus,
)
from patchfrog.publishing.errors import PublicationFailureClass
from patchfrog.publishing.github_publisher import GitHubClientReviewPublisher
from patchfrog.publishing.service import (
    ReviewNotFoundError,
    ReviewPublicationService,
    ReviewRunNotAssociatedWithPullRequestError,
)

logger = structlog.get_logger(__name__)


class RetryablePublicationError(RuntimeError):
    """Raised to signal Celery's autoretry machinery -- never raised
    across the public :func:`_publish_review` boundary otherwise."""


async def _publish_review(
    *, review_run_id: uuid.UUID, mode: ReviewPublicationMode, settings: Settings
) -> ReviewPublicationResult:
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

            async with session_factory() as session:
                # Phase 7 (patchfrog.review_memory) suppression -- derived
                # entirely from review_run_id, independent of whatever the
                # review task passed through in-process, so a publish
                # retry/redelivery always recomputes it fresh.
                already_reported_finding_ids = (
                    await ReviewMemoryFindingRepository().list_carried_forward_current_finding_ids(
                        session, review_run_id=review_run_id
                    )
                )

            service = ReviewPublicationService(session_factory=session_factory, publisher=publisher)
            return await service.publish(
                review_run_id=review_run_id,
                mode=mode,
                config=config,
                already_reported_finding_ids=already_reported_finding_ids,
            )
    finally:
        await engine.dispose()


@celery_app.task(  # type: ignore[untyped-decorator]
    name="patchfrog.publish_review",
    autoretry_for=(RetryablePublicationError,),
    max_retries=5,
    retry_backoff=30,
    retry_backoff_max=600,
    retry_jitter=True,
)
def publish_review_task(*, review_run_id: str, publish: bool = False) -> str:
    mode = ReviewPublicationMode.PUBLISH if publish else ReviewPublicationMode.DRY_RUN
    settings = get_settings()

    try:
        result = asyncio.run(
            _publish_review(review_run_id=uuid.UUID(review_run_id), mode=mode, settings=settings)
        )
    except (ReviewNotFoundError, ReviewRunNotAssociatedWithPullRequestError) as exc:
        # Non-retryable: no amount of retrying makes a missing run or a
        # run with no associated PR publishable.
        raise Reject(str(exc), requeue=False) from exc

    logger.info(
        "publish_review_task_completed",
        review_run_id=review_run_id,
        status=result.status.value,
        mode=mode.value,
        published_inline=result.published_inline,
        reconciled=result.reconciled,
    )

    if result.status is ReviewPublicationStatus.FAILED and result.errors:
        # The persisted error string already carries its own
        # classification prefix (see patchfrog.publishing.service._fail's
        # f"{failure_class.value}: {detail}") -- retry (via `autoretry_for`
        # above) only on the classes that can plausibly succeed on a
        # later attempt.
        raw = result.errors[0]
        retryable_prefixes = (f"{PublicationFailureClass.RETRYABLE.value}:", f"{PublicationFailureClass.RATE_LIMITED.value}:")
        if raw.startswith(retryable_prefixes):
            raise RetryablePublicationError(raw)

    return (
        f"status={result.status.value} mode={mode.value} "
        f"published_inline={result.published_inline} summary_only={result.summary_only} "
        f"omitted={result.omitted} reconciled={result.reconciled}"
    )
