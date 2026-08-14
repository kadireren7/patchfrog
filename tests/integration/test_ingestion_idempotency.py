"""Proves that re-processing the same GitHub webhook delivery is a safe no-op.

GitHub may redeliver a webhook (e.g. after a timeout on their side even
though PatchFrog actually processed it). The unique constraint on
``delivery_id`` is what guarantees a retried delivery never causes
duplicate work.
"""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from patchfrog.persistence.models import PullRequestIngestionModel
from patchfrog.persistence.repositories.pull_request_ingestion import PullRequestIngestionRepository
from patchfrog.services.pull_request_ingestion import (
    IngestionOutcomeStatus,
    PullRequestIngestionService,
)
from tests.integration.test_pull_request_ingestion_service import (
    CHANGED_FILES,
    EVENT,
    PR_METADATA,
    _FakeGitHubClient,
)


async def test_reserve_rejects_duplicate_delivery_id(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    repo = PullRequestIngestionRepository()

    async with session_factory() as session:
        first = await repo.reserve(
            session, delivery_id="dup-delivery", event_action="opened", head_sha="abc"
        )
        await session.commit()

    assert first is not None

    async with session_factory() as session:
        second = await repo.reserve(
            session, delivery_id="dup-delivery", event_action="opened", head_sha="abc"
        )

    assert second is None


async def test_duplicate_webhook_delivery_is_a_noop(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    fake_client = _FakeGitHubClient(metadata=PR_METADATA, files=CHANGED_FILES)
    service = PullRequestIngestionService(
        session_factory=session_factory,
        github_client=fake_client,  # type: ignore[arg-type]
    )

    first_outcome = await service.ingest(EVENT)
    second_outcome = await service.ingest(EVENT)

    assert first_outcome.status is IngestionOutcomeStatus.SUCCEEDED
    assert second_outcome.status is IngestionOutcomeStatus.DUPLICATE
    assert fake_client.get_pull_request_calls == 1

    async with session_factory() as session:
        count = (
            await session.execute(
                select(func.count())
                .select_from(PullRequestIngestionModel)
                .where(PullRequestIngestionModel.delivery_id == EVENT.delivery_id)
            )
        ).scalar_one()
        assert count == 1
