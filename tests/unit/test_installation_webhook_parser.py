from __future__ import annotations

import copy
from typing import Any

import pytest

from patchfrog.domain.github import (
    InstallationEventAction,
    InstallationRepositoriesEventAction,
)
from patchfrog.github.errors import WebhookPayloadError
from patchfrog.github.webhooks import (
    parse_installation_event,
    parse_installation_repositories_event,
)

_INSTALLATION_PAYLOAD: dict[str, Any] = {
    "action": "created",
    "installation": {"id": 55667788, "account": {"login": "kadireren7", "type": "User"}},
}

_INSTALLATION_REPOSITORIES_PAYLOAD: dict[str, Any] = {
    "action": "added",
    "installation": {"id": 55667788, "account": {"login": "kadireren7", "type": "User"}},
    "repositories_added": [{"id": 1, "full_name": "kadireren7/libft"}],
    "repositories_removed": [],
}


def test_parses_installation_created() -> None:
    event = parse_installation_event(
        event_name="installation", delivery_id="delivery-1", payload=_INSTALLATION_PAYLOAD
    )

    assert event is not None
    assert event.action is InstallationEventAction.CREATED
    assert event.installation.id == 55667788
    assert event.account.login == "kadireren7"
    assert event.account.account_type == "User"


def test_installation_unsupported_action_is_ignored() -> None:
    payload = copy.deepcopy(_INSTALLATION_PAYLOAD)
    payload["action"] = "new_permissions_accepted"

    event = parse_installation_event(
        event_name="installation", delivery_id="delivery-2", payload=payload
    )

    assert event is None


def test_installation_unsupported_event_type_is_ignored() -> None:
    event = parse_installation_event(
        event_name="pull_request", delivery_id="delivery-3", payload=_INSTALLATION_PAYLOAD
    )

    assert event is None


def test_installation_missing_action_is_ignored() -> None:
    payload = copy.deepcopy(_INSTALLATION_PAYLOAD)
    del payload["action"]

    event = parse_installation_event(
        event_name="installation", delivery_id="delivery-4", payload=payload
    )

    assert event is None


def test_installation_missing_account_raises() -> None:
    payload = copy.deepcopy(_INSTALLATION_PAYLOAD)
    del payload["installation"]["account"]

    with pytest.raises(WebhookPayloadError):
        parse_installation_event(
            event_name="installation", delivery_id="delivery-5", payload=payload
        )


def test_parses_installation_repositories_added() -> None:
    event = parse_installation_repositories_event(
        event_name="installation_repositories",
        delivery_id="delivery-6",
        payload=_INSTALLATION_REPOSITORIES_PAYLOAD,
    )

    assert event is not None
    assert event.action is InstallationRepositoriesEventAction.ADDED
    assert event.installation.id == 55667788
    assert len(event.repositories_added) == 1
    assert event.repositories_added[0].github_repository_id == 1
    assert event.repositories_added[0].full_name == "kadireren7/libft"
    assert event.repositories_removed == ()


def test_installation_repositories_unsupported_event_type_is_ignored() -> None:
    event = parse_installation_repositories_event(
        event_name="installation",
        delivery_id="delivery-7",
        payload=_INSTALLATION_REPOSITORIES_PAYLOAD,
    )

    assert event is None


def test_installation_repositories_missing_action_is_ignored() -> None:
    payload = copy.deepcopy(_INSTALLATION_REPOSITORIES_PAYLOAD)
    del payload["action"]

    event = parse_installation_repositories_event(
        event_name="installation_repositories", delivery_id="delivery-8", payload=payload
    )

    assert event is None


def test_installation_repositories_missing_installation_raises() -> None:
    payload = copy.deepcopy(_INSTALLATION_REPOSITORIES_PAYLOAD)
    del payload["installation"]

    with pytest.raises(WebhookPayloadError):
        parse_installation_repositories_event(
            event_name="installation_repositories", delivery_id="delivery-9", payload=payload
        )
