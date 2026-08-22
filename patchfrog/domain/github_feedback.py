"""Internal domain models for GitHub feedback surfaces (Phase 9): pull
request review comments, their reactions, and review-thread resolution
state.

Mirrors :mod:`patchfrog.domain.pull_request` and
:mod:`patchfrog.domain.github_review`'s role exactly: the stable boundary
between raw GitHub JSON (REST or GraphQL) and the rest of PatchFrog.
Nothing here does I/O -- translation happens at
:mod:`patchfrog.github.client`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class GitHubActorType(StrEnum):
    """GitHub's own ``user.type`` discriminator. The only signal Phase 9
    uses to filter out bot actors (including PatchFrog's own bot identity)
    -- never a hardcoded login/slug comparison, since any bot actor's
    reaction/reply is equally not real developer feedback."""

    USER = "User"
    BOT = "Bot"
    ORGANIZATION = "Organization"


@dataclass(frozen=True, slots=True)
class GitHubActor:
    """Minimal actor identity -- login and type only. Never more than
    this is persisted (see the module docstring of
    :mod:`patchfrog.feedback.domain` on privacy minimization)."""

    login: str
    actor_type: GitHubActorType


@dataclass(frozen=True, slots=True)
class GitHubReviewComment:
    """One pull request review (inline) comment, as returned by
    ``GET /repos/{owner}/{repo}/pulls/{number}/comments`` -- covers both
    PatchFrog's own published comments and developer replies to them
    (``in_reply_to_id`` set)."""

    id: int
    path: str
    line: int | None
    original_line: int | None
    side: str | None
    body: str
    actor: GitHubActor
    in_reply_to_id: int | None
    pull_request_review_id: int | None
    created_at: datetime
    updated_at: datetime


class GitHubReactionContent(StrEnum):
    """GitHub's fixed reaction vocabulary. Every raw value is preserved
    verbatim on the persisted event -- see
    :mod:`patchfrog.feedback.domain`'s ``normalize_reaction`` for why this
    is never collapsed to a bare +/-1 internally."""

    PLUS_ONE = "+1"
    MINUS_ONE = "-1"
    LAUGH = "laugh"
    HOORAY = "hooray"
    CONFUSED = "confused"
    HEART = "heart"
    ROCKET = "rocket"
    EYES = "eyes"


@dataclass(frozen=True, slots=True)
class GitHubReaction:
    """One reaction on a review comment, as returned by
    ``GET /repos/{owner}/{repo}/pulls/comments/{comment_id}/reactions``."""

    id: int
    content: GitHubReactionContent
    actor: GitHubActor
    created_at: datetime


@dataclass(frozen=True, slots=True)
class GitHubReviewThreadStatus:
    """One review thread's resolution state, from the GraphQL
    ``reviewThreads`` connection -- REST has no equivalent field.
    ``first_comment_id`` is the (REST) database id of the thread's first
    comment, the join key back to PatchFrog's own published comment."""

    first_comment_id: int | None
    is_resolved: bool
