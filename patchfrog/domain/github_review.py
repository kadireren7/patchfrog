"""Internal domain models for publishing a GitHub Pull Request Review.

Translated GitHub REST shapes only -- mirrors :mod:`patchfrog.domain.pull_request`'s
role as the boundary between raw GitHub JSON and the rest of PatchFrog.
Nothing in :mod:`patchfrog.publishing` (the pure planner/domain layer)
imports from here; only :mod:`patchfrog.github.client` and
:mod:`patchfrog.publishing.github_publisher` (the transport boundary) do.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class GitHubReviewEvent(StrEnum):
    """The subset of GitHub's pull request review ``event`` values PatchFrog
    is allowed to submit. Deliberately excludes ``APPROVE`` and
    ``REQUEST_CHANGES`` -- Phase 6 never renders a merge-affecting verdict;
    that is an explicit later-phase product/policy decision, not a
    publishing-mechanics one."""

    COMMENT = "COMMENT"


class GitHubDiffSide(StrEnum):
    """GitHub's own vocabulary for which half of a diff a position refers
    to. Kept out of the pure planner/domain models (see
    :class:`patchfrog.publishing.domain.DiffSide`) -- translated at the
    transport boundary only."""

    LEFT = "LEFT"
    RIGHT = "RIGHT"


@dataclass(frozen=True, slots=True)
class GitHubReviewCommentInput:
    """One inline comment to submit as part of a pull request review."""

    path: str
    body: str
    line: int
    side: GitHubDiffSide
    start_line: int | None = None
    start_side: GitHubDiffSide | None = None


@dataclass(frozen=True, slots=True)
class GitHubSubmittedReview:
    """A pull request review as returned by the GitHub API, whether just
    submitted or discovered during reconciliation (see
    :meth:`patchfrog.github.client.GitHubClient.list_pull_request_reviews`)."""

    id: int
    body: str | None
    state: str
    commit_id: str | None
    user_login: str | None
