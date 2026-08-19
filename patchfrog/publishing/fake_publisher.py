"""Deterministic, in-memory :class:`~patchfrog.publishing.github_publisher.ReviewPublisher`
for tests.

Mirrors :class:`patchfrog.review.providers.fake.FakeLLMProvider`'s role
exactly -- structural typing via ``Protocol`` (see
:mod:`patchfrog.publishing.github_publisher`) is what makes this a
legitimate stand-in for :mod:`patchfrog.publishing.service` tests rather
than a mock of internal plumbing. Simulates a minimal in-memory "GitHub":
tracks submitted reviews (so :meth:`find_patchfrog_review` reconciliation
can actually find them) and records every call for assertions.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from patchfrog.domain.github_review import (
    GitHubReviewCommentInput,
    GitHubReviewEvent,
    GitHubSubmittedReview,
)
from patchfrog.domain.pull_request import ChangedFile, PullRequestMetadata, PullRequestRef
from patchfrog.publishing.marker import find_marker


@dataclass(frozen=True, slots=True)
class RecordedPublishCall:
    ref: PullRequestRef
    commit_id: str
    body: str
    event: GitHubReviewEvent
    comments: tuple[GitHubReviewCommentInput, ...]


class FakeReviewPublisher:
    """Not a subclass of anything -- satisfies
    :class:`patchfrog.publishing.github_publisher.ReviewPublisher`
    structurally."""

    def __init__(
        self,
        *,
        pull_request: PullRequestMetadata,
        changed_files: list[ChangedFile] | None = None,
        head_sha_sequence: list[str] | None = None,
        publish_exception: Exception | None = None,
        diff_exception: Exception | None = None,
    ) -> None:
        self._pull_request = pull_request
        self._changed_files = changed_files or []
        self._head_sha_sequence = list(head_sha_sequence) if head_sha_sequence else None
        self._publish_exception = publish_exception
        self._diff_exception = diff_exception
        self._next_review_id = 9000
        self.existing_reviews: list[GitHubSubmittedReview] = []
        self.publish_calls: list[RecordedPublishCall] = []
        self.head_sha_call_count = 0

    async def get_pull_request(self, ref: PullRequestRef) -> PullRequestMetadata:
        return self._pull_request

    async def get_head_sha(self, ref: PullRequestRef) -> str:
        self.head_sha_call_count += 1
        if self._head_sha_sequence:
            index = min(self.head_sha_call_count - 1, len(self._head_sha_sequence) - 1)
            return self._head_sha_sequence[index]
        return self._pull_request.head_sha

    async def get_pull_request_diff(self, ref: PullRequestRef) -> list[ChangedFile]:
        if self._diff_exception is not None:
            raise self._diff_exception
        return self._changed_files

    async def find_patchfrog_review(
        self, ref: PullRequestRef, *, publication_id: UUID
    ) -> GitHubSubmittedReview | None:
        for review in self.existing_reviews:
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
        self.publish_calls.append(
            RecordedPublishCall(ref=ref, commit_id=commit_id, body=body, event=event, comments=tuple(comments))
        )
        if self._publish_exception is not None:
            raise self._publish_exception

        review = GitHubSubmittedReview(
            id=self._next_review_id, body=body, state=event.value, commit_id=commit_id, user_login="patchfrog-bot"
        )
        self._next_review_id += 1
        self.existing_reviews.append(review)
        return review
