"""Narrow GitHub boundary for migration PR publication (M9.9).

Mirrors :mod:`patchfrog.publishing.github_publisher`'s role exactly:
structural typing via ``Protocol`` names only the operations
:mod:`patchfrog.migration_pr.publisher` actually needs, so a fake
satisfying this Protocol is a legitimate stand-in for tests -- never a
mock of internal plumbing, and no real GitHub PR is ever opened by an
automated test (M9.9's own requirement).
"""

from __future__ import annotations

from typing import Protocol

from patchfrog.domain.github_check import GitHubCheckRun, GitHubCheckRunInput
from patchfrog.domain.github_git import GitTreeEntry
from patchfrog.domain.pull_request import PullRequestMetadata
from patchfrog.github.client import GitHubClient
from patchfrog.migration_pr.check import MigrationCheckPublisher


class MigrationGitHubPublisher(Protocol):
    async def get_ref(self, *, owner: str, repository: str, ref: str) -> str | None: ...

    async def create_ref(self, *, owner: str, repository: str, ref: str, sha: str) -> None: ...

    async def update_ref(self, *, owner: str, repository: str, ref: str, sha: str, force: bool = False) -> None: ...

    async def get_commit_tree_sha(self, *, owner: str, repository: str, commit_sha: str) -> str: ...

    async def create_tree(
        self, *, owner: str, repository: str, base_tree_sha: str, entries: list[GitTreeEntry]
    ) -> str: ...

    async def create_commit(
        self, *, owner: str, repository: str, message: str, tree_sha: str, parent_sha: str
    ) -> str: ...

    async def create_pull_request(
        self, *, owner: str, repository: str, title: str, body: str, head: str, base: str, draft: bool = False
    ) -> PullRequestMetadata: ...

    async def list_pull_requests(
        self, *, owner: str, repository: str, head: str | None = None, state: str = "all"
    ) -> list[PullRequestMetadata]: ...

    async def update_pull_request(
        self, *, owner: str, repository: str, number: int, title: str | None = None, body: str | None = None
    ) -> PullRequestMetadata: ...

    async def reconcile_check(
        self, *, owner: str, repository: str, head_sha: str, check: GitHubCheckRunInput
    ) -> GitHubCheckRun: ...


class GitHubClientMigrationPublisher:
    """The real :class:`MigrationGitHubPublisher` -- a thin,
    installation-scoped wrapper around :class:`GitHubClient`, exactly the
    role :class:`patchfrog.publishing.github_publisher.GitHubClientReviewPublisher`
    plays for review publishing."""

    def __init__(self, *, github_client: GitHubClient, installation_id: int) -> None:
        self._client = github_client
        self._installation_id = installation_id
        self._check_publisher = MigrationCheckPublisher(client=github_client, installation_id=installation_id)

    async def get_ref(self, *, owner: str, repository: str, ref: str) -> str | None:
        return await self._client.get_ref(installation_id=self._installation_id, owner=owner, repository=repository, ref=ref)

    async def create_ref(self, *, owner: str, repository: str, ref: str, sha: str) -> None:
        await self._client.create_ref(
            installation_id=self._installation_id, owner=owner, repository=repository, ref=ref, sha=sha,
        )

    async def update_ref(self, *, owner: str, repository: str, ref: str, sha: str, force: bool = False) -> None:
        await self._client.update_ref(
            installation_id=self._installation_id, owner=owner, repository=repository, ref=ref, sha=sha, force=force,
        )

    async def get_commit_tree_sha(self, *, owner: str, repository: str, commit_sha: str) -> str:
        return await self._client.get_commit_tree_sha(
            installation_id=self._installation_id, owner=owner, repository=repository, commit_sha=commit_sha,
        )

    async def create_tree(
        self, *, owner: str, repository: str, base_tree_sha: str, entries: list[GitTreeEntry]
    ) -> str:
        return await self._client.create_tree(
            installation_id=self._installation_id, owner=owner, repository=repository, base_tree_sha=base_tree_sha,
            entries=entries,
        )

    async def create_commit(
        self, *, owner: str, repository: str, message: str, tree_sha: str, parent_sha: str
    ) -> str:
        return await self._client.create_commit(
            installation_id=self._installation_id, owner=owner, repository=repository, message=message,
            tree_sha=tree_sha, parent_sha=parent_sha,
        )

    async def create_pull_request(
        self, *, owner: str, repository: str, title: str, body: str, head: str, base: str, draft: bool = False
    ) -> PullRequestMetadata:
        return await self._client.create_pull_request(
            installation_id=self._installation_id, owner=owner, repository=repository, title=title, body=body,
            head=head, base=base, draft=draft,
        )

    async def list_pull_requests(
        self, *, owner: str, repository: str, head: str | None = None, state: str = "all"
    ) -> list[PullRequestMetadata]:
        return await self._client.list_pull_requests(
            installation_id=self._installation_id, owner=owner, repository=repository, head=head, state=state,
        )

    async def update_pull_request(
        self, *, owner: str, repository: str, number: int, title: str | None = None, body: str | None = None
    ) -> PullRequestMetadata:
        return await self._client.update_pull_request(
            installation_id=self._installation_id, owner=owner, repository=repository, number=number, title=title,
            body=body,
        )

    async def reconcile_check(
        self, *, owner: str, repository: str, head_sha: str, check: GitHubCheckRunInput
    ) -> GitHubCheckRun:
        return await self._check_publisher.reconcile(owner=owner, repository=repository, head_sha=head_sha, check=check)


__all__ = ["GitHubClientMigrationPublisher", "MigrationGitHubPublisher"]
