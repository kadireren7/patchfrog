"""Orchestrates syncing GitHub App installation lifecycle events into
persisted state -- the account-level counterpart to
:mod:`patchfrog.services.pull_request_ingestion`.

This is the *only* place ``installations``/``repositories.is_selected``
are ever written from a webhook. A repository row itself is still only
ever created lazily by the first real PR event (see
:class:`~patchfrog.persistence.repositories.repository.RepositoryRepository.upsert`,
called from :mod:`patchfrog.services.pull_request_ingestion`) -- an
``installation_repositories`` "added" event for a repository PatchFrog
has never seen yet has nothing to flip (a newly-created row already
defaults ``is_selected=True``); a "removed" event for one never seen has
nothing to stop.
"""

from __future__ import annotations

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from patchfrog.domain.github import (
    InstallationEventAction,
    InstallationRepositoriesWebhookEvent,
    InstallationWebhookEvent,
)
from patchfrog.persistence.models.installation import (
    BetaState,
    InstallationModel,
    InstallationStatus,
)
from patchfrog.persistence.repositories import InstallationRepository, RepositoryRepository

logger = structlog.get_logger(__name__)

_STATUS_BY_ACTION: dict[InstallationEventAction, InstallationStatus] = {
    InstallationEventAction.DELETED: InstallationStatus.DELETED,
    InstallationEventAction.SUSPEND: InstallationStatus.SUSPENDED,
    InstallationEventAction.UNSUSPEND: InstallationStatus.ACTIVE,
}


class InstallationSyncService:
    def __init__(self, *, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory
        self._installation_repo = InstallationRepository()
        self._repository_repo = RepositoryRepository()

    async def sync_installation_event(
        self, event: InstallationWebhookEvent, *, allowlist_mode: bool
    ) -> InstallationModel:
        async with self._session_factory() as session:
            # `upsert` always self-heals first -- creates the row if this
            # is the first event PatchFrog has ever seen for this
            # installation (covers both a real `created` event and a
            # lifecycle-only event arriving first, e.g. PatchFrog was
            # offline for the original `created` delivery), or just
            # refreshes login/type on an existing row.
            model = await self._installation_repo.upsert(
                session,
                github_installation_id=event.installation.id,
                account_login=event.account.login,
                account_type=event.account.account_type,
                default_beta_state=BetaState.PENDING if allowlist_mode else BetaState.ACTIVE,
            )
            target_status = _STATUS_BY_ACTION.get(event.action)
            if target_status is not None:
                model.status = target_status
                await session.flush()
            await session.commit()

        logger.info(
            "installation_event_synced",
            github_installation_id=event.installation.id,
            action=event.action.value,
            account_login=event.account.login,
        )
        return model

    async def sync_installation_repositories_event(self, event: InstallationRepositoriesWebhookEvent) -> None:
        async with self._session_factory() as session:
            # Self-heal the installation row too -- see the class docstring.
            await self._installation_repo.upsert(
                session,
                github_installation_id=event.installation.id,
                account_login=event.account.login,
                account_type=event.account.account_type,
            )
            for repo in event.repositories_added:
                await self._repository_repo.set_selected(
                    session, github_repository_id=repo.github_repository_id, selected=True
                )
            for repo in event.repositories_removed:
                await self._repository_repo.set_selected(
                    session, github_repository_id=repo.github_repository_id, selected=False
                )
            await session.commit()

        logger.info(
            "installation_repositories_event_synced",
            github_installation_id=event.installation.id,
            action=event.action.value,
            added=[r.full_name for r in event.repositories_added],
            removed=[r.full_name for r in event.repositories_removed],
        )
