"""Integration coverage for :class:`InstallationSyncService` against a
real (in-memory SQLite) session -- installation lifecycle events and
repository selection changes, driven by webhook-shaped domain events
exactly as ``apps/worker/tasks/sync_installation.py`` constructs them.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from patchfrog.domain.github import (
    InstallationAccountRef,
    InstallationEventAction,
    InstallationRef,
    InstallationRepositoriesEventAction,
    InstallationRepositoriesWebhookEvent,
    InstallationRepositoryStub,
    InstallationWebhookEvent,
)
from patchfrog.persistence.models.installation import BetaState, InstallationStatus
from patchfrog.persistence.repositories import InstallationRepository, RepositoryRepository
from patchfrog.services.installation_sync import InstallationSyncService

_ACCOUNT = InstallationAccountRef(login="kadireren7", account_type="User")


async def test_created_event_self_heals_a_new_installation_row(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    service = InstallationSyncService(session_factory=session_factory)
    event = InstallationWebhookEvent(
        delivery_id="d1",
        action=InstallationEventAction.CREATED,
        installation=InstallationRef(id=55667788),
        account=_ACCOUNT,
    )

    model = await service.sync_installation_event(event, allowlist_mode=False)

    assert model.github_installation_id == 55667788
    assert model.status is InstallationStatus.ACTIVE
    assert model.beta_state is BetaState.ACTIVE


async def test_allowlist_mode_starts_a_new_installation_pending(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    service = InstallationSyncService(session_factory=session_factory)
    event = InstallationWebhookEvent(
        delivery_id="d2",
        action=InstallationEventAction.CREATED,
        installation=InstallationRef(id=11223344),
        account=_ACCOUNT,
    )

    model = await service.sync_installation_event(event, allowlist_mode=True)

    assert model.beta_state is BetaState.PENDING


async def test_suspend_then_unsuspend_flips_status_on_the_existing_row(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    service = InstallationSyncService(session_factory=session_factory)
    installation_id = 99887766
    await service.sync_installation_event(
        InstallationWebhookEvent(
            delivery_id="d3",
            action=InstallationEventAction.CREATED,
            installation=InstallationRef(id=installation_id),
            account=_ACCOUNT,
        ),
        allowlist_mode=False,
    )

    suspended = await service.sync_installation_event(
        InstallationWebhookEvent(
            delivery_id="d4",
            action=InstallationEventAction.SUSPEND,
            installation=InstallationRef(id=installation_id),
            account=_ACCOUNT,
        ),
        allowlist_mode=False,
    )
    assert suspended.status is InstallationStatus.SUSPENDED

    unsuspended = await service.sync_installation_event(
        InstallationWebhookEvent(
            delivery_id="d5",
            action=InstallationEventAction.UNSUSPEND,
            installation=InstallationRef(id=installation_id),
            account=_ACCOUNT,
        ),
        allowlist_mode=False,
    )
    assert unsuspended.status is InstallationStatus.ACTIVE


async def test_deleted_event_on_a_never_before_seen_installation_self_heals_then_marks_deleted(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A `deleted` event can legitimately arrive without a prior `created`
    delivery -- the service must still record it, not crash or ignore it."""

    service = InstallationSyncService(session_factory=session_factory)
    model = await service.sync_installation_event(
        InstallationWebhookEvent(
            delivery_id="d6",
            action=InstallationEventAction.DELETED,
            installation=InstallationRef(id=44556677),
            account=_ACCOUNT,
        ),
        allowlist_mode=False,
    )

    assert model.status is InstallationStatus.DELETED


async def test_repositories_added_and_removed_flip_is_selected(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    installation_id = 22334455
    repo_repo = RepositoryRepository()
    async with session_factory() as session:
        repo = await repo_repo.upsert(
            session,
            github_repository_id=1,
            owner="kadireren7",
            name="libft",
            full_name="kadireren7/libft",
            installation_id=installation_id,
        )
        repo.is_selected = False
        await session.commit()

    service = InstallationSyncService(session_factory=session_factory)
    await service.sync_installation_repositories_event(
        InstallationRepositoriesWebhookEvent(
            delivery_id="d7",
            action=InstallationRepositoriesEventAction.ADDED,
            installation=InstallationRef(id=installation_id),
            account=_ACCOUNT,
            repositories_added=(InstallationRepositoryStub(github_repository_id=1, full_name="kadireren7/libft"),),
            repositories_removed=(),
        )
    )

    async with session_factory() as session:
        refreshed = await repo_repo.get_by_github_id(session, github_repository_id=1)
        assert refreshed is not None
        assert refreshed.is_selected is True

    await service.sync_installation_repositories_event(
        InstallationRepositoriesWebhookEvent(
            delivery_id="d8",
            action=InstallationRepositoriesEventAction.REMOVED,
            installation=InstallationRef(id=installation_id),
            account=_ACCOUNT,
            repositories_added=(),
            repositories_removed=(InstallationRepositoryStub(github_repository_id=1, full_name="kadireren7/libft"),),
        )
    )

    async with session_factory() as session:
        refreshed = await repo_repo.get_by_github_id(session, github_repository_id=1)
        assert refreshed is not None
        assert refreshed.is_selected is False

    async with session_factory() as session:
        installation = await InstallationRepository().get_by_github_id(
            session, github_installation_id=installation_id
        )
        assert installation is not None, "the installation row must self-heal too"


async def test_repositories_removed_for_a_never_seen_repository_is_a_safe_no_op(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    service = InstallationSyncService(session_factory=session_factory)

    await service.sync_installation_repositories_event(
        InstallationRepositoriesWebhookEvent(
            delivery_id="d9",
            action=InstallationRepositoriesEventAction.REMOVED,
            installation=InstallationRef(id=66778899),
            account=_ACCOUNT,
            repositories_added=(),
            repositories_removed=(InstallationRepositoryStub(github_repository_id=999, full_name="ghost/repo"),),
        )
    )
