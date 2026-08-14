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
from patchfrog.persistence.database import create_engine, create_session_factory
from patchfrog.services.pull_request_ingestion import IngestionOutcome, PullRequestIngestionService

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
            return await service.ingest(event)
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
