"""Celery tasks: sync GitHub App installation lifecycle events.

Thin adapters only, mirroring
:mod:`apps.worker.tasks.process_pull_request`'s shape exactly --
reconstructs the domain event from primitive task arguments (Celery task
arguments must be JSON-serializable, so the API layer never passes a
dataclass directly) and delegates all real work to
:class:`patchfrog.services.installation_sync.InstallationSyncService`.
Kept as Celery tasks (not handled inline in the API process) to preserve
the existing architectural boundary: the API process never touches the
database directly (see :mod:`apps.api.dependencies`'s module docstring).
"""

from __future__ import annotations

import asyncio
from typing import Any

import structlog

from apps.worker.celery_app import celery_app
from patchfrog.config.settings import Settings, get_settings
from patchfrog.domain.github import (
    InstallationAccountRef,
    InstallationEventAction,
    InstallationRef,
    InstallationRepositoriesEventAction,
    InstallationRepositoriesWebhookEvent,
    InstallationRepositoryStub,
    InstallationWebhookEvent,
)
from patchfrog.persistence.database import create_engine, create_session_factory
from patchfrog.services.installation_sync import InstallationSyncService

logger = structlog.get_logger(__name__)


async def _sync_installation(event: InstallationWebhookEvent, settings: Settings) -> None:
    engine = create_engine(settings.database_url)
    session_factory = create_session_factory(engine)
    try:
        service = InstallationSyncService(session_factory=session_factory)
        await service.sync_installation_event(event, allowlist_mode=settings.beta_allowlist_mode)
    finally:
        await engine.dispose()


async def _sync_installation_repositories(event: InstallationRepositoriesWebhookEvent, settings: Settings) -> None:
    engine = create_engine(settings.database_url)
    session_factory = create_session_factory(engine)
    try:
        service = InstallationSyncService(session_factory=session_factory)
        await service.sync_installation_repositories_event(event)
    finally:
        await engine.dispose()


@celery_app.task(name="patchfrog.sync_installation_event")  # type: ignore[untyped-decorator]
def sync_installation_event_task(
    *, delivery_id: str, action: str, github_installation_id: int, account_login: str, account_type: str
) -> str:
    event = InstallationWebhookEvent(
        delivery_id=delivery_id,
        action=InstallationEventAction(action),
        installation=InstallationRef(id=github_installation_id),
        account=InstallationAccountRef(login=account_login, account_type=account_type),
    )
    asyncio.run(_sync_installation(event, get_settings()))
    logger.info("installation_sync_task_completed", github_installation_id=github_installation_id, action=action)
    return "synced"


@celery_app.task(name="patchfrog.sync_installation_repositories_event")  # type: ignore[untyped-decorator]
def sync_installation_repositories_event_task(
    *,
    delivery_id: str,
    action: str,
    github_installation_id: int,
    account_login: str,
    account_type: str,
    repositories_added: list[dict[str, Any]],
    repositories_removed: list[dict[str, Any]],
) -> str:
    def _stub(item: dict[str, Any]) -> InstallationRepositoryStub:
        return InstallationRepositoryStub(
            github_repository_id=int(item["id"]),
            full_name=str(item["full_name"]),
        )

    event = InstallationRepositoriesWebhookEvent(
        delivery_id=delivery_id,
        action=InstallationRepositoriesEventAction(action),
        installation=InstallationRef(id=github_installation_id),
        account=InstallationAccountRef(login=account_login, account_type=account_type),
        repositories_added=tuple(_stub(r) for r in repositories_added),
        repositories_removed=tuple(_stub(r) for r in repositories_removed),
    )
    asyncio.run(_sync_installation_repositories(event, get_settings()))
    logger.info(
        "installation_repositories_sync_task_completed",
        github_installation_id=github_installation_id,
        action=action,
    )
    return "synced"
