"""Parsing of verified GitHub webhook payloads into domain events.

Only called *after* :func:`patchfrog.github.signatures.verify_signature` has
confirmed the request is authentic. Unsupported event types/actions are
ignored safely (return ``None``); a supported event with missing critical
fields raises :class:`~patchfrog.github.errors.WebhookPayloadError`.
"""

from __future__ import annotations

from typing import Any

from patchfrog.domain.github import (
    InstallationRef,
    PullRequestEventAction,
    PullRequestWebhookEvent,
    RepositoryRef,
)
from patchfrog.github.errors import WebhookPayloadError

_PULL_REQUEST_EVENT = "pull_request"


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
