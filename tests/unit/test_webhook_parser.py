from __future__ import annotations

import copy
from typing import Any

import pytest

from patchfrog.domain.github import PullRequestEventAction
from patchfrog.github.errors import WebhookPayloadError
from patchfrog.github.webhooks import parse_pull_request_event


def test_parses_opened_pull_request(fixture_loader: Any) -> None:
    payload = fixture_loader("pull_request_opened.json")

    event = parse_pull_request_event(
        event_name="pull_request", delivery_id="delivery-1", payload=payload
    )

    assert event is not None
    assert event.action is PullRequestEventAction.OPENED
    assert event.delivery_id == "delivery-1"
    assert event.repository.owner == "kadireren7"
    assert event.repository.name == "libft"
    assert event.repository.full_name == "kadireren7/libft"
    assert event.repository.installation.id == 55667788
    assert event.pull_request_number == 14
    assert event.pull_request_title == "Add libft ft_strdup implementation"
    assert event.author == "kadireren7"
    assert event.base_branch == "main"
    assert event.head_branch == "feature/ft-strdup"
    assert event.base_sha == "111aaa2222bbb3333ccc4444ddd5555eeee6666"
    assert event.head_sha == "8ad712fa9c1e4b6d8f0a2b3c4d5e6f708192a3b4"
    assert event.html_url == "https://github.com/kadireren7/libft/pull/14"


def test_parses_synchronize_pull_request(fixture_loader: Any) -> None:
    payload = fixture_loader("pull_request_synchronize.json")

    event = parse_pull_request_event(
        event_name="pull_request", delivery_id="delivery-2", payload=payload
    )

    assert event is not None
    assert event.action is PullRequestEventAction.SYNCHRONIZE
    assert event.head_sha == "9be823fb0d2f5c7e9a1b3c4d5e6f708192a3b4c5"


def test_parses_reopened_pull_request(fixture_loader: Any) -> None:
    payload = copy.deepcopy(fixture_loader("pull_request_opened.json"))
    payload["action"] = "reopened"

    event = parse_pull_request_event(
        event_name="pull_request", delivery_id="delivery-3", payload=payload
    )

    assert event is not None
    assert event.action is PullRequestEventAction.REOPENED


def test_parses_closed_pull_request_not_merged(fixture_loader: Any) -> None:
    payload = fixture_loader("pull_request_closed.json")

    event = parse_pull_request_event(
        event_name="pull_request", delivery_id="delivery-closed", payload=payload
    )

    assert event is not None
    assert event.action is PullRequestEventAction.CLOSED
    assert event.merged is False


def test_parses_closed_pull_request_merged(fixture_loader: Any) -> None:
    payload = copy.deepcopy(fixture_loader("pull_request_closed.json"))
    payload["pull_request"]["merged"] = True

    event = parse_pull_request_event(
        event_name="pull_request", delivery_id="delivery-merged", payload=payload
    )

    assert event is not None
    assert event.action is PullRequestEventAction.CLOSED
    assert event.merged is True


def test_merged_defaults_false_when_absent_from_payload(fixture_loader: Any) -> None:
    """GitHub always sends `merged` on `pull_request` payloads in practice,
    but the parser must not crash if it's ever missing -- `merged` isn't a
    "critical field" that should raise WebhookPayloadError."""

    payload = fixture_loader("pull_request_opened.json")
    assert "merged" not in payload["pull_request"]

    event = parse_pull_request_event(
        event_name="pull_request", delivery_id="delivery-no-merged", payload=payload
    )

    assert event is not None
    assert event.merged is False


def test_unsupported_action_is_ignored(fixture_loader: Any) -> None:
    payload = copy.deepcopy(fixture_loader("pull_request_opened.json"))
    payload["action"] = "labeled"

    event = parse_pull_request_event(
        event_name="pull_request", delivery_id="delivery-4", payload=payload
    )

    assert event is None


def test_unsupported_event_type_is_ignored(fixture_loader: Any) -> None:
    payload = fixture_loader("pull_request_opened.json")

    event = parse_pull_request_event(
        event_name="issue_comment", delivery_id="delivery-5", payload=payload
    )

    assert event is None


def test_missing_critical_field_raises(fixture_loader: Any) -> None:
    payload = copy.deepcopy(fixture_loader("pull_request_opened.json"))
    del payload["pull_request"]["base"]

    with pytest.raises(WebhookPayloadError):
        parse_pull_request_event(
            event_name="pull_request", delivery_id="delivery-6", payload=payload
        )


def test_missing_installation_raises(fixture_loader: Any) -> None:
    payload = copy.deepcopy(fixture_loader("pull_request_opened.json"))
    del payload["installation"]

    with pytest.raises(WebhookPayloadError):
        parse_pull_request_event(
            event_name="pull_request", delivery_id="delivery-7", payload=payload
        )


def test_missing_action_is_ignored(fixture_loader: Any) -> None:
    payload = copy.deepcopy(fixture_loader("pull_request_opened.json"))
    del payload["action"]

    event = parse_pull_request_event(
        event_name="pull_request", delivery_id="delivery-8", payload=payload
    )

    assert event is None
