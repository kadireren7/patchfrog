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


class InstallationEventAction(StrEnum):
    """The subset of GitHub `installation` webhook actions PatchFrog acts
    on. Deliberately excludes `new_permissions_accepted` -- it carries no
    lifecycle-state change PatchFrog needs to track."""

    CREATED = "created"
    DELETED = "deleted"
    SUSPEND = "suspend"
    UNSUSPEND = "unsuspend"


@dataclass(frozen=True, slots=True)
class InstallationAccountRef:
    """The account (user or organization) a GitHub App installation
    belongs to."""

    login: str
    account_type: str


@dataclass(frozen=True, slots=True)
class InstallationWebhookEvent:
    """A normalized `installation` webhook event -- installed, uninstalled,
    suspended, or unsuspended."""

    delivery_id: str
    action: InstallationEventAction
    installation: InstallationRef
    account: InstallationAccountRef


class InstallationRepositoriesEventAction(StrEnum):
    ADDED = "added"
    REMOVED = "removed"


@dataclass(frozen=True, slots=True)
class InstallationRepositoryStub:
    """A minimal repository reference as it appears inside
    `installation_repositories`' `repositories_added`/`repositories_removed`
    arrays -- GitHub does not include `owner`/`installation` there, unlike
    the fuller :class:`RepositoryRef`."""

    github_repository_id: int
    full_name: str


@dataclass(frozen=True, slots=True)
class InstallationRepositoriesWebhookEvent:
    """A normalized `installation_repositories` webhook event -- a
    "selected" installation's repository list changed."""

    delivery_id: str
    action: InstallationRepositoriesEventAction
    installation: InstallationRef
    account: InstallationAccountRef
    repositories_added: tuple[InstallationRepositoryStub, ...]
    repositories_removed: tuple[InstallationRepositoryStub, ...]
