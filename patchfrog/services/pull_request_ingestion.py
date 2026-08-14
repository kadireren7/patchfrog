"""Orchestrates ingesting a single pull request webhook event.

This is the only place that ties together the GitHub API client, the diff
parser, and persistence for a webhook delivery. The Celery task
(:mod:`apps.worker.tasks.process_pull_request`) is a thin adapter around
:class:`PullRequestIngestionService.ingest`.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from patchfrog.diff.parser import build_diff_file
from patchfrog.domain.github import PullRequestWebhookEvent
from patchfrog.domain.pull_request import PullRequestRef
from patchfrog.github.client import GitHubClient
from patchfrog.github.errors import GitHubError
from patchfrog.persistence.repositories import (
    PullRequestIngestionRepository,
    PullRequestRepository,
    RepositoryRepository,
)

logger = structlog.get_logger(__name__)


class IngestionOutcomeStatus(StrEnum):
    """Result of a single call to :meth:`PullRequestIngestionService.ingest`."""

    DUPLICATE = "duplicate"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class IngestionOutcome:
    status: IngestionOutcomeStatus
    delivery_id: str
    repository_full_name: str
    pull_request_number: int
    head_sha: str | None = None
    changed_files_count: int | None = None
    additions: int | None = None
    deletions: int | None = None
    error_message: str | None = None


class PullRequestIngestionService:
    """Fetches, normalizes, and persists a pull request webhook event."""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        github_client: GitHubClient,
    ) -> None:
        self._session_factory = session_factory
        self._github_client = github_client
        self._ingestion_repo = PullRequestIngestionRepository()
        self._repository_repo = RepositoryRepository()
        self._pull_request_repo = PullRequestRepository()

    async def ingest(self, event: PullRequestWebhookEvent) -> IngestionOutcome:
        base_log = logger.bind(
            github_delivery_id=event.delivery_id,
            repository=event.repository.full_name,
            pull_request_number=event.pull_request_number,
            event_action=event.action.value,
        )

        async with self._session_factory() as session:
            ingestion = await self._ingestion_repo.reserve(
                session,
                delivery_id=event.delivery_id,
                event_action=event.action.value,
                head_sha=event.head_sha,
            )
            if ingestion is None:
                base_log.info("pull_request_ingestion_duplicate")
                return IngestionOutcome(
                    status=IngestionOutcomeStatus.DUPLICATE,
                    delivery_id=event.delivery_id,
                    repository_full_name=event.repository.full_name,
                    pull_request_number=event.pull_request_number,
                )
            await session.commit()
            ingestion_id = ingestion.id

        ref = PullRequestRef(
            owner=event.repository.owner,
            repository=event.repository.name,
            number=event.pull_request_number,
        )

        try:
            metadata = await self._github_client.get_pull_request(
                installation_id=event.repository.installation.id, ref=ref
            )
            changed_files = await self._github_client.list_pull_request_files(
                installation_id=event.repository.installation.id, ref=ref
            )
        except GitHubError as exc:
            async with self._session_factory() as session:
                await self._ingestion_repo.mark_failed(
                    session, ingestion_id=ingestion_id, error_message=str(exc)
                )
                await session.commit()
            base_log.error("pull_request_ingestion_failed", error=str(exc))
            return IngestionOutcome(
                status=IngestionOutcomeStatus.FAILED,
                delivery_id=event.delivery_id,
                repository_full_name=event.repository.full_name,
                pull_request_number=event.pull_request_number,
                error_message=str(exc),
            )

        # Normalize each file's patch into the internal diff representation.
        # Phase 1 only needs this to compute summary counts for the log line;
        # future phases will persist/query the parsed hunks directly.
        diff_files = [build_diff_file(f.path, f.patch) for f in changed_files]
        total_additions = sum(f.additions for f in changed_files)
        total_deletions = sum(f.deletions for f in changed_files)

        async with self._session_factory() as session:
            repository_row = await self._repository_repo.upsert(
                session,
                github_repository_id=event.repository.github_repository_id,
                owner=event.repository.owner,
                name=event.repository.name,
                full_name=event.repository.full_name,
                installation_id=event.repository.installation.id,
            )
            pull_request_row = await self._pull_request_repo.upsert(
                session,
                repository_id=repository_row.id,
                github_pr_number=metadata.number,
                title=metadata.title,
                author=metadata.author,
                base_sha=metadata.base_sha,
                head_sha=metadata.head_sha,
                state=metadata.state,
            )
            await self._ingestion_repo.mark_succeeded(
                session, ingestion_id=ingestion_id, pull_request_id=pull_request_row.id
            )
            await session.commit()

        base_log.info(
            "pull_request_ingested",
            head_sha=metadata.head_sha,
            files=len(diff_files),
            additions=total_additions,
            deletions=total_deletions,
        )

        return IngestionOutcome(
            status=IngestionOutcomeStatus.SUCCEEDED,
            delivery_id=event.delivery_id,
            repository_full_name=event.repository.full_name,
            pull_request_number=event.pull_request_number,
            head_sha=metadata.head_sha,
            changed_files_count=len(diff_files),
            additions=total_additions,
            deletions=total_deletions,
        )
