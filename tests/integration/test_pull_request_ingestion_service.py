from __future__ import annotations

import dataclasses

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from patchfrog.domain.github import (
    InstallationRef,
    PullRequestEventAction,
    PullRequestWebhookEvent,
    RepositoryRef,
)
from patchfrog.domain.pull_request import ChangedFile, FileChangeStatus, PullRequestMetadata
from patchfrog.github.errors import GitHubNotFoundError
from patchfrog.persistence.models import (
    IngestionStatus,
    PullRequestIngestionModel,
    PullRequestModel,
    RepositoryModel,
)
from patchfrog.services.pull_request_ingestion import (
    IngestionOutcomeStatus,
    PullRequestIngestionService,
)

EVENT = PullRequestWebhookEvent(
    delivery_id="delivery-abc",
    action=PullRequestEventAction.OPENED,
    repository=RepositoryRef(
        github_repository_id=987654321,
        owner="kadireren7",
        name="libft",
        full_name="kadireren7/libft",
        installation=InstallationRef(id=55667788),
    ),
    pull_request_number=14,
    pull_request_title="Add ft_strdup (webhook title)",
    pull_request_body="desc",
    author="kadireren7",
    base_branch="main",
    head_branch="feature/ft-strdup",
    base_sha="aaa111",
    head_sha="bbb222",
    html_url="https://github.com/kadireren7/libft/pull/14",
)

PR_METADATA = PullRequestMetadata(
    number=14,
    title="Add ft_strdup implementation",
    body="desc",
    author="kadireren7",
    base_branch="main",
    head_branch="feature/ft-strdup",
    base_sha="aaa111",
    head_sha="ccc333",
    html_url="https://github.com/kadireren7/libft/pull/14",
    state="open",
)

CHANGED_FILES = [
    ChangedFile(
        path="ft_strdup.c",
        previous_path=None,
        status=FileChangeStatus.ADDED,
        additions=14,
        deletions=0,
        patch="@@ -0,0 +1,2 @@\n+line one\n+line two",
    ),
    ChangedFile(
        path="Makefile",
        previous_path=None,
        status=FileChangeStatus.MODIFIED,
        additions=1,
        deletions=1,
        patch="@@ -10,1 +10,1 @@\n-old\n+new",
    ),
]


class _FakeGitHubClient:
    def __init__(self, *, metadata: PullRequestMetadata, files: list[ChangedFile]) -> None:
        self._metadata = metadata
        self._files = files
        self.get_pull_request_calls = 0

    async def get_pull_request(self, *, installation_id: int, ref: object) -> PullRequestMetadata:
        self.get_pull_request_calls += 1
        return self._metadata

    async def list_pull_request_files(
        self, *, installation_id: int, ref: object
    ) -> list[ChangedFile]:
        return self._files


class _FailingGitHubClient:
    async def get_pull_request(self, *, installation_id: int, ref: object) -> PullRequestMetadata:
        raise GitHubNotFoundError("pull request not found")

    async def list_pull_request_files(
        self, *, installation_id: int, ref: object
    ) -> list[ChangedFile]:
        raise GitHubNotFoundError("pull request not found")


async def test_ingest_persists_repository_pull_request_and_ingestion(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    fake_client = _FakeGitHubClient(metadata=PR_METADATA, files=CHANGED_FILES)
    service = PullRequestIngestionService(
        session_factory=session_factory,
        github_client=fake_client,  # type: ignore[arg-type]
    )

    outcome = await service.ingest(EVENT)

    assert outcome.status is IngestionOutcomeStatus.SUCCEEDED
    assert outcome.changed_files_count == 2
    assert outcome.additions == 15
    assert outcome.deletions == 1
    # The GitHub API response is authoritative, not the (potentially stale) webhook payload.
    assert outcome.head_sha == "ccc333"

    async with session_factory() as session:
        repo = (await session.execute(select(RepositoryModel))).scalar_one()
        assert repo.github_repository_id == 987654321
        assert repo.full_name == "kadireren7/libft"

        pr = (await session.execute(select(PullRequestModel))).scalar_one()
        assert pr.repository_id == repo.id
        assert pr.github_pr_number == 14
        assert pr.title == "Add ft_strdup implementation"
        assert pr.head_sha == "ccc333"

        ingestion = (await session.execute(select(PullRequestIngestionModel))).scalar_one()
        assert ingestion.status is IngestionStatus.SUCCEEDED
        assert ingestion.pull_request_id == pr.id
        assert ingestion.delivery_id == "delivery-abc"
        assert ingestion.completed_at is not None


async def test_ingest_records_failure_when_github_api_fails(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    service = PullRequestIngestionService(
        session_factory=session_factory,
        github_client=_FailingGitHubClient(),  # type: ignore[arg-type]
    )

    outcome = await service.ingest(EVENT)

    assert outcome.status is IngestionOutcomeStatus.FAILED
    assert outcome.error_message is not None

    async with session_factory() as session:
        ingestion = (await session.execute(select(PullRequestIngestionModel))).scalar_one()
        assert ingestion.status is IngestionStatus.FAILED
        assert ingestion.pull_request_id is None
        assert ingestion.error_message is not None

        # A failed GitHub fetch must not create repository/PR rows.
        repos = (await session.execute(select(RepositoryModel))).scalars().all()
        assert repos == []


async def test_ingestion_status_is_persisted_as_lowercase_value(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Regression: SQLAlchemy's Enum type defaults to persisting the Python
    enum *member name* (``SUCCEEDED``) rather than its ``.value``
    (``succeeded``) unless ``values_callable`` is set. Assert the raw
    stored string directly, bypassing the ORM's enum round-trip, since
    that round-trip would mask the bug either way.
    """

    fake_client = _FakeGitHubClient(metadata=PR_METADATA, files=CHANGED_FILES)
    service = PullRequestIngestionService(
        session_factory=session_factory,
        github_client=fake_client,  # type: ignore[arg-type]
    )

    await service.ingest(EVENT)

    async with session_factory() as session:
        raw_status = (
            await session.execute(text("SELECT status FROM pull_request_ingestions"))
        ).scalar_one()

    assert raw_status == "succeeded"


async def test_ingest_upserts_existing_repository_and_pull_request(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    fake_client = _FakeGitHubClient(metadata=PR_METADATA, files=CHANGED_FILES)
    service = PullRequestIngestionService(
        session_factory=session_factory,
        github_client=fake_client,  # type: ignore[arg-type]
    )

    await service.ingest(EVENT)
    second_event = dataclasses.replace(EVENT, delivery_id="delivery-def")
    await service.ingest(second_event)

    async with session_factory() as session:
        repos = (await session.execute(select(RepositoryModel))).scalars().all()
        prs = (await session.execute(select(PullRequestModel))).scalars().all()
        assert len(repos) == 1
        assert len(prs) == 1
