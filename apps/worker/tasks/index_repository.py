"""Celery task: index a repository at a specific commit SHA.

Thin adapter only — resolves the installation access token and internal
repository row, then delegates all real work to
:class:`patchfrog.indexing.service.RepositoryIndexingService`. Kept as
its own task (not folded into ``patchfrog.process_pull_request_event``)
so PR ingestion and repository indexing scale and fail independently.

Nothing here triggers automatically on a webhook yet — for Phase 2 this
is invoked explicitly (CLI, or a future task caller), per the "controlled
trigger" requirement.
"""

from __future__ import annotations

import asyncio

import httpx
import structlog

from apps.worker.celery_app import celery_app
from patchfrog.config.settings import Settings, get_settings
from patchfrog.github.auth import InstallationTokenProvider
from patchfrog.indexing.models import IndexingSummary
from patchfrog.indexing.service import RepositoryIndexingService
from patchfrog.persistence.database import create_engine, create_session_factory
from patchfrog.persistence.repositories import RepositoryRepository

logger = structlog.get_logger(__name__)


async def _index(
    *,
    github_repository_id: int,
    owner: str,
    name: str,
    full_name: str,
    installation_id: int,
    commit_sha: str,
    settings: Settings,
) -> IndexingSummary:
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

        async with httpx.AsyncClient(timeout=settings.github_api_timeout_seconds) as http_client:
            token_provider = InstallationTokenProvider(
                http_client=http_client,
                app_id=settings.github_app_id,
                private_key=settings.github_private_key,
                api_base_url=settings.github_api_base_url,
            )
            token = await token_provider.get_token(installation_id)

        service = RepositoryIndexingService(session_factory=session_factory)
        return await service.index_repository(
            repository_id=repository_id,
            clone_url=f"https://github.com/{full_name}.git",
            commit_sha=commit_sha,
            repository_full_name=full_name,
            token=token,
        )
    finally:
        await engine.dispose()


@celery_app.task(name="patchfrog.index_repository")  # type: ignore[untyped-decorator]
def index_repository_task(
    *,
    github_repository_id: int,
    owner: str,
    name: str,
    full_name: str,
    installation_id: int,
    commit_sha: str,
) -> str:
    summary = asyncio.run(
        _index(
            github_repository_id=github_repository_id,
            owner=owner,
            name=name,
            full_name=full_name,
            installation_id=installation_id,
            commit_sha=commit_sha,
            settings=get_settings(),
        )
    )
    logger.info(
        "repository_index_task_completed",
        repository=full_name,
        commit_sha=commit_sha,
        files_total=summary.files_total,
        symbols_extracted=summary.symbols_extracted,
        edges_created=summary.edges_created,
        incremental=summary.incremental,
    )
    return "succeeded"
