"""Internal domain models describing GitHub App installations and webhook
events.

These models are the stable internal representation of "a relevant thing
happened on GitHub". Nothing here depends on the raw GitHub JSON schema,
FastAPI, Celery, or SQLAlchemy — translation from raw webhook payloads
happens at the GitHub boundary (:mod:`patchfrog.github.webhooks`).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class PullRequestEventAction(StrEnum):
    """The subset of GitHub `pull_request` webhook actions PatchFrog acts on."""

    OPENED = "opened"
    REOPENED = "reopened"
    SYNCHRONIZE = "synchronize"


@dataclass(frozen=True, slots=True)
class InstallationRef:
    """Reference to a GitHub App installation."""

    id: int


@dataclass(frozen=True, slots=True)
class RepositoryRef:
    """Reference to a GitHub repository, as seen by an installation."""

    github_repository_id: int
    owner: str
    name: str
    full_name: str
    installation: InstallationRef


@dataclass(frozen=True, slots=True)
class PullRequestWebhookEvent:
    """A normalized, supported `pull_request` webhook event.

    Produced by :mod:`patchfrog.github.webhooks` after signature verification
    and payload validation. This is the only shape the rest of PatchFrog
    should ever reason about when handling a webhook delivery.
    """

    delivery_id: str
    action: PullRequestEventAction
    repository: RepositoryRef
    pull_request_number: int
    pull_request_title: str
    pull_request_body: str | None
    author: str
    base_branch: str
    head_branch: str
    base_sha: str
    head_sha: str
    html_url: str
