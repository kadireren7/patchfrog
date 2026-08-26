"""Parsing of verified GitHub webhook payloads into domain events.

Only called *after* :func:`patchfrog.github.signatures.verify_signature` has
confirmed the request is authentic. Unsupported event types/actions are
ignored safely (return ``None``); a supported event with missing critical
fields raises :class:`~patchfrog.github.errors.WebhookPayloadError`.
"""

from __future__ import annotations

from typing import Any

from patchfrog.domain.github import (
    InstallationAccountRef,
    InstallationEventAction,
    InstallationRef,
    InstallationRepositoriesEventAction,
    InstallationRepositoriesWebhookEvent,
    InstallationRepositoryStub,
    InstallationWebhookEvent,
    PullRequestEventAction,
    PullRequestWebhookEvent,
    RepositoryRef,
)
from patchfrog.github.errors import WebhookPayloadError

_PULL_REQUEST_EVENT = "pull_request"
_INSTALLATION_EVENT = "installation"
_INSTALLATION_REPOSITORIES_EVENT = "installation_repositories"


def parse_pull_request_event(
    *, event_name: str, delivery_id: str, payload: dict[str, Any]
) -> PullRequestWebhookEvent | None:
    """Parse a webhook delivery into a :class:`PullRequestWebhookEvent`.

    Returns ``None`` when the event type or action isn't one PatchFrog acts
    on (safe to ignore). Raises :class:`WebhookPayloadError` when the event
    *is* one we act on but the payload is missing fields we require.
    """

    if event_name != _PULL_REQUEST_EVENT:
        return None

    action_value = payload.get("action")
    if not isinstance(action_value, str):
        return None
    try:
        action = PullRequestEventAction(action_value)
    except ValueError:
        return None

    try:
        pull_request = payload["pull_request"]
        repository = payload["repository"]
        installation = payload["installation"]

        return PullRequestWebhookEvent(
            delivery_id=delivery_id,
            action=action,
            repository=RepositoryRef(
                github_repository_id=repository["id"],
                owner=repository["owner"]["login"],
                name=repository["name"],
                full_name=repository["full_name"],
                installation=InstallationRef(id=installation["id"]),
            ),
            pull_request_number=pull_request["number"],
            pull_request_title=pull_request["title"],
            pull_request_body=pull_request.get("body"),
            author=pull_request["user"]["login"],
            base_branch=pull_request["base"]["ref"],
            head_branch=pull_request["head"]["ref"],
            base_sha=pull_request["base"]["sha"],
            head_sha=pull_request["head"]["sha"],
            html_url=pull_request["html_url"],
        )
    except (KeyError, TypeError) as exc:
        raise WebhookPayloadError(
            f"Malformed pull_request webhook payload (delivery={delivery_id}): missing {exc}"
        ) from exc


def parse_installation_event(
    *, event_name: str, delivery_id: str, payload: dict[str, Any]
) -> InstallationWebhookEvent | None:
    """Parse a webhook delivery into an :class:`InstallationWebhookEvent`
    (App installed / uninstalled / suspended / unsuspended). Returns
    ``None`` for an event type/action PatchFrog doesn't act on."""

    if event_name != _INSTALLATION_EVENT:
        return None

    action_value = payload.get("action")
    if not isinstance(action_value, str):
        return None
    try:
        action = InstallationEventAction(action_value)
    except ValueError:
        return None

    try:
        installation = payload["installation"]
        account = installation["account"]
        return InstallationWebhookEvent(
            delivery_id=delivery_id,
            action=action,
            installation=InstallationRef(id=installation["id"]),
            account=InstallationAccountRef(
                login=account["login"], account_type=account.get("type", "User")
            ),
        )
    except (KeyError, TypeError) as exc:
        raise WebhookPayloadError(
            f"Malformed installation webhook payload (delivery={delivery_id}): missing {exc}"
        ) from exc


def _parse_repository_stub(item: dict[str, Any]) -> InstallationRepositoryStub:
    return InstallationRepositoryStub(github_repository_id=item["id"], full_name=item["full_name"])


def parse_installation_repositories_event(
    *, event_name: str, delivery_id: str, payload: dict[str, Any]
) -> InstallationRepositoriesWebhookEvent | None:
    """Parse a webhook delivery into an
    :class:`InstallationRepositoriesWebhookEvent` (a "selected"
    installation's repository list changed). Returns ``None`` for an
    event type/action PatchFrog doesn't act on."""

    if event_name != _INSTALLATION_REPOSITORIES_EVENT:
        return None

    action_value = payload.get("action")
    if not isinstance(action_value, str):
        return None
    try:
        action = InstallationRepositoriesEventAction(action_value)
    except ValueError:
        return None

    try:
        installation = payload["installation"]
        account = installation["account"]
        added = [_parse_repository_stub(item) for item in payload.get("repositories_added", [])]
        removed = [_parse_repository_stub(item) for item in payload.get("repositories_removed", [])]
        return InstallationRepositoriesWebhookEvent(
            delivery_id=delivery_id,
            action=action,
            installation=InstallationRef(id=installation["id"]),
            account=InstallationAccountRef(
                login=account["login"], account_type=account.get("type", "User")
            ),
            repositories_added=tuple(added),
            repositories_removed=tuple(removed),
        )
    except (KeyError, TypeError) as exc:
        raise WebhookPayloadError(
            f"Malformed installation_repositories webhook payload (delivery={delivery_id}): missing {exc}"
        ) from exc
