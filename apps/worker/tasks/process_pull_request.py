"""Celery task: ingest a single pull request webhook event.

Thin adapter only — reconstructs the domain event from primitive task
arguments and delegates all real work to
:class:`patchfrog.services.pull_request_ingestion.PullRequestIngestionService`.
"""

from __future__ import annotations

import asyncio

import httpx
import structlog

from apps.worker.celery_app import celery_app
from patchfrog.config.settings import Settings, get_settings
from patchfrog.domain.github import (
    InstallationRef,
    PullRequestEventAction,
    PullRequestWebhookEvent,
    RepositoryRef,
)
from patchfrog.github.auth import InstallationTokenProvider
from patchfrog.github.client import GitHubClient
from patchfrog.ops import metrics
from patchfrog.ops.orchestrator import schedule_pipeline_if_eligible
from patchfrog.persistence.database import create_engine, create_session_factory
from patchfrog.services.pull_request_ingestion import (
    IngestionOutcome,
    IngestionOutcomeStatus,
    PullRequestIngestionService,
)

logger = structlog.get_logger(__name__)


def _reconstruct_event(
    *,
    delivery_id: str,
    action: str,
    github_repository_id: int,
    owner: str,
    name: str,
    full_name: str,
    installation_id: int,
    pull_request_number: int,
    pull_request_title: str,
    pull_request_body: str | None,
    author: str,
    base_branch: str,
    head_branch: str,
    base_sha: str,
    head_sha: str,
    html_url: str,
) -> PullRequestWebhookEvent:
    """Rebuild the domain event from the primitive arguments Celery serialized."""

    return PullRequestWebhookEvent(
        delivery_id=delivery_id,
        action=PullRequestEventAction(action),
        repository=RepositoryRef(
            github_repository_id=github_repository_id,
            owner=owner,
            name=name,
            full_name=full_name,
            installation=InstallationRef(id=installation_id),
        ),
        pull_request_number=pull_request_number,
        pull_request_title=pull_request_title,
        pull_request_body=pull_request_body,
        author=author,
        base_branch=base_branch,
        head_branch=head_branch,
        base_sha=base_sha,
        head_sha=head_sha,
        html_url=html_url,
    )


async def _ingest(event: PullRequestWebhookEvent, settings: Settings) -> IngestionOutcome:
    engine = create_engine(settings.database_url)
    session_factory = create_session_factory(engine)

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
            service = PullRequestIngestionService(
                session_factory=session_factory, github_client=github_client
            )
            outcome = await service.ingest(event)

        if outcome.status is IngestionOutcomeStatus.SUCCEEDED:
            # Only opened/reopened/synchronize ever reach here (see
            # patchfrog.github.webhooks.parse_pull_request_event) -- every
            # one of those actions means "there is a commit that should
            # be reviewed", so scheduling is unconditional on the action
            # itself; patchfrog.ops.eligibility is what actually decides
            # whether this specific installation/repository/PR may
            # proceed.
            #
            # This call must never be allowed to propagate: ingestion's
            # delivery_id uniqueness constraint means a re-delivered (or
            # Celery-retried) webhook for an already-SUCCEEDED ingestion
            # is recognized as a DUPLICATE and short-circuits before
            # reaching this line again -- so a transient failure here
            # (e.g. Redis briefly unreachable) would otherwise leave a
            # successfully-ingested PR that silently never gets
            # reviewed, undetectable by `ops failed`/`ops stale` (both
            # only ever look at `review_runs`, and no such row would
            # exist). Caught, logged with everything needed to manually
            # recover, and surfaced on the one metric built for exactly
            # this shape of outcome instead.
            try:
                await schedule_pipeline_if_eligible(
                    session_factory,
                    settings=settings,
                    repository_ref=event.repository,
                    commit_sha=event.head_sha,
                    pull_request_number=event.pull_request_number,
                )
            except Exception as exc:
                logger.error(
                    "pipeline_scheduling_failed",
                    github_delivery_id=event.delivery_id,
                    repository=event.repository.full_name,
                    pull_request_number=event.pull_request_number,
                    commit_sha=event.head_sha,
                    error=str(exc),
                )
                metrics.reviews_skipped_total.labels(reason="scheduling_failed").inc()

        return outcome
    finally:
        await engine.dispose()


@celery_app.task(name="patchfrog.process_pull_request_event")  # type: ignore[untyped-decorator]
def process_pull_request_event(
    *,
    delivery_id: str,
    action: str,
    github_repository_id: int,
    owner: str,
    name: str,
    full_name: str,
    installation_id: int,
    pull_request_number: int,
    pull_request_title: str,
    pull_request_body: str | None,
    author: str,
    base_branch: str,
    head_branch: str,
    base_sha: str,
    head_sha: str,
    html_url: str,
) -> str:
    event = _reconstruct_event(
        delivery_id=delivery_id,
        action=action,
        github_repository_id=github_repository_id,
        owner=owner,
        name=name,
        full_name=full_name,
        installation_id=installation_id,
        pull_request_number=pull_request_number,
        pull_request_title=pull_request_title,
        pull_request_body=pull_request_body,
        author=author,
        base_branch=base_branch,
        head_branch=head_branch,
        base_sha=base_sha,
        head_sha=head_sha,
        html_url=html_url,
    )

    outcome = asyncio.run(_ingest(event, get_settings()))
    return outcome.status.value
