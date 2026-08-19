"""Narrow GitHub review-publishing boundary.

Everything :mod:`patchfrog.publishing.service` needs from GitHub, and
nothing else -- transport/API specifics (REST paths, JSON shapes,
pagination) stay behind :class:`patchfrog.github.client.GitHubClient`;
:class:`ReviewPublisher` only names the four operations publication
actually needs. Structural typing via ``Protocol`` (mirrors
:class:`patchfrog.review.provider.LLMProvider`) is the point of the
abstraction -- it's what makes
:class:`patchfrog.review.providers.fake.FakeLLMProvider`'s counterpart
here, a fake review publisher, a legitimate stand-in for tests rather
than a mock of internal plumbing. The planner
(:mod:`patchfrog.publishing.planner`) never imports this module at all,
which is what keeps *it* fully unit-testable without network access.
"""

from __future__ import annotations

from typing import Protocol
from uuid import UUID

from patchfrog.domain.github_review import (
    GitHubReviewCommentInput,
    GitHubReviewEvent,
    GitHubSubmittedReview,
)
from patchfrog.domain.pull_request import ChangedFile, PullRequestMetadata, PullRequestRef
from patchfrog.github.client import GitHubClient
from patchfrog.publishing.marker import find_marker


class ReviewPublisher(Protocol):
    """Provider-neutral (well, GitHub-neutral) review-publishing
    interface. Implementations: :class:`GitHubClientReviewPublisher`
    (real) and test fakes."""

    async def get_pull_request(self, ref: PullRequestRef) -> PullRequestMetadata: ...

    async def get_head_sha(self, ref: PullRequestRef) -> str: ...

    async def get_pull_request_diff(self, ref: PullRequestRef) -> list[ChangedFile]: ...

    async def find_patchfrog_review(
        self, ref: PullRequestRef, *, publication_id: UUID
    ) -> GitHubSubmittedReview | None: ...

    async def publish_review(
        self,
        *,
        ref: PullRequestRef,
        commit_id: str,
        body: str,
        event: GitHubReviewEvent,
        comments: list[GitHubReviewCommentInput],
    ) -> GitHubSubmittedReview: ...


class GitHubClientReviewPublisher:
    """Thin, installation-scoped wrapper around :class:`GitHubClient` for
    the review-publishing use case -- the real :class:`ReviewPublisher`."""

    def __init__(self, *, github_client: GitHubClient, installation_id: int) -> None:
        self._client = github_client
        self._installation_id = installation_id

    async def get_pull_request(self, ref: PullRequestRef) -> PullRequestMetadata:
        return await self._client.get_pull_request(installation_id=self._installation_id, ref=ref)

    async def get_head_sha(self, ref: PullRequestRef) -> str:
        """A fresh, live read of the PR's current head -- never a cached
        or stored value. This is the "current_github_head_sha" half of
        stale-head protection (see :mod:`patchfrog.publishing.service`)."""

        metadata = await self.get_pull_request(ref)
        return metadata.head_sha

    async def get_pull_request_diff(self, ref: PullRequestRef) -> list[ChangedFile]:
        return await self._client.list_pull_request_files(installation_id=self._installation_id, ref=ref)

    async def find_patchfrog_review(
        self, ref: PullRequestRef, *, publication_id: UUID
    ) -> GitHubSubmittedReview | None:
        """Scan the PR's existing reviews for one carrying PatchFrog's own
        marker for this exact ``publication_id`` (see
        :mod:`patchfrog.publishing.marker`). Used for reconciliation: a
        process that crashed between the GitHub write and the following
        DB commit can find its own already-published review here on retry
        instead of writing a duplicate (see
        :mod:`patchfrog.publishing.service`)."""

        reviews = await self._client.list_pull_request_reviews(installation_id=self._installation_id, ref=ref)
        for review in reviews:
            if find_marker(review.body) == publication_id:
                return review
        return None

    async def publish_review(
        self,
        *,
        ref: PullRequestRef,
        commit_id: str,
        body: str,
        event: GitHubReviewEvent,
        comments: list[GitHubReviewCommentInput],
    ) -> GitHubSubmittedReview:
        return await self._client.create_pull_request_review(
            installation_id=self._installation_id,
            ref=ref,
            commit_id=commit_id,
            event=event,
            body=body,
            comments=comments,
        )
