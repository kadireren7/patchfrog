"""Unit coverage for GitHubClient's Phase 9 feedback methods
(list_pull_request_review_comments, list_review_comment_reactions,
list_review_thread_statuses, and get_pull_request's ``merged`` field) --
mocked GitHub API via respx, no live network. Mirrors
tests/unit/test_github_client_reviews.py's conventions exactly."""

from __future__ import annotations

from collections.abc import AsyncIterator

import httpx
import pytest
import respx

from patchfrog.domain.github_feedback import GitHubActorType, GitHubReactionContent
from patchfrog.domain.pull_request import PullRequestRef
from patchfrog.github.client import GitHubClient
from patchfrog.github.errors import GitHubResponseError

API_BASE = "https://api.github.com"
REF = PullRequestRef(owner="kadireren7", repository="libft", number=14)


class _StubTokenProvider:
    async def get_token(self, installation_id: int) -> str:
        return "stub-installation-token"


def _make_client(http_client: httpx.AsyncClient) -> GitHubClient:
    return GitHubClient(
        http_client=http_client,
        token_provider=_StubTokenProvider(),  # type: ignore[arg-type]
        api_base_url=API_BASE,
        timeout_seconds=5.0,
    )


@pytest.fixture
async def http_client() -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient() as client:
        yield client


@respx.mock
async def test_list_pull_request_review_comments_parses_bot_and_reply(http_client: httpx.AsyncClient) -> None:
    respx.get(f"{API_BASE}/repos/kadireren7/libft/pulls/14/comments").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "id": 1,
                    "path": "a.py",
                    "line": 10,
                    "original_line": 10,
                    "side": "RIGHT",
                    "body": "finding body",
                    "user": {"login": "patchfrog[bot]", "type": "Bot"},
                    "in_reply_to_id": None,
                    "pull_request_review_id": 999,
                    "created_at": "2026-01-01T00:00:00Z",
                    "updated_at": "2026-01-01T00:00:00Z",
                },
                {
                    "id": 2,
                    "path": "a.py",
                    "line": 10,
                    "original_line": 10,
                    "side": "RIGHT",
                    "body": "/patchfrog useful",
                    "user": {"login": "developer", "type": "User"},
                    "in_reply_to_id": 1,
                    "pull_request_review_id": None,
                    "created_at": "2026-01-01T01:00:00Z",
                    "updated_at": "2026-01-01T01:00:00Z",
                },
            ],
        )
    )
    client = _make_client(http_client)
    comments = await client.list_pull_request_review_comments(installation_id=1, ref=REF)
    assert len(comments) == 2
    assert comments[0].actor.actor_type is GitHubActorType.BOT
    assert comments[1].in_reply_to_id == 1
    assert comments[1].actor.actor_type is GitHubActorType.USER


@respx.mock
async def test_list_review_comment_reactions_filters_unknown_content(http_client: httpx.AsyncClient) -> None:
    respx.get(f"{API_BASE}/repos/kadireren7/libft/pulls/comments/1/reactions").mock(
        return_value=httpx.Response(
            200,
            json=[
                {"id": 10, "content": "+1", "user": {"login": "dev", "type": "User"}, "created_at": "2026-01-01T00:00:00Z"},
                {"id": 11, "content": "surprised-pikachu", "user": {"login": "dev", "type": "User"}, "created_at": "2026-01-01T00:00:00Z"},
            ],
        )
    )
    client = _make_client(http_client)
    reactions = await client.list_review_comment_reactions(installation_id=1, ref=REF, comment_id=1)
    assert len(reactions) == 1
    assert reactions[0].content is GitHubReactionContent.PLUS_ONE


@respx.mock
async def test_list_review_thread_statuses_parses_graphql_response(http_client: httpx.AsyncClient) -> None:
    respx.post(f"{API_BASE}/graphql").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": {
                    "repository": {
                        "pullRequest": {
                            "reviewThreads": {
                                "pageInfo": {"hasNextPage": False, "endCursor": None},
                                "nodes": [
                                    {"isResolved": True, "comments": {"nodes": [{"databaseId": 1}]}},
                                    {"isResolved": False, "comments": {"nodes": [{"databaseId": 2}]}},
                                ],
                            }
                        }
                    }
                }
            },
        )
    )
    client = _make_client(http_client)
    statuses = await client.list_review_thread_statuses(installation_id=1, ref=REF)
    assert len(statuses) == 2
    assert statuses[0].first_comment_id == 1
    assert statuses[0].is_resolved is True
    assert statuses[1].is_resolved is False


@respx.mock
async def test_graphql_errors_raise_response_error(http_client: httpx.AsyncClient) -> None:
    respx.post(f"{API_BASE}/graphql").mock(
        return_value=httpx.Response(200, json={"data": None, "errors": [{"message": "not found"}]})
    )
    client = _make_client(http_client)
    with pytest.raises(GitHubResponseError):
        await client.list_review_thread_statuses(installation_id=1, ref=REF)


@respx.mock
async def test_get_pull_request_parses_merged_field(http_client: httpx.AsyncClient) -> None:
    respx.get(f"{API_BASE}/repos/kadireren7/libft/pulls/14").mock(
        return_value=httpx.Response(
            200,
            json={
                "number": 14,
                "title": "t",
                "body": None,
                "user": {"login": "dev"},
                "base": {"ref": "main", "sha": "base"},
                "head": {"ref": "feature", "sha": "head"},
                "html_url": "https://github.com/x/y/pull/14",
                "state": "closed",
                "merged": True,
            },
        )
    )
    client = _make_client(http_client)
    metadata = await client.get_pull_request(installation_id=1, ref=REF)
    assert metadata.merged is True
    assert metadata.state == "closed"


@respx.mock
async def test_get_pull_request_defaults_merged_to_false_when_absent(http_client: httpx.AsyncClient) -> None:
    respx.get(f"{API_BASE}/repos/kadireren7/libft/pulls/14").mock(
        return_value=httpx.Response(
            200,
            json={
                "number": 14,
                "title": "t",
                "body": None,
                "user": {"login": "dev"},
                "base": {"ref": "main", "sha": "base"},
                "head": {"ref": "feature", "sha": "head"},
                "html_url": "https://github.com/x/y/pull/14",
                "state": "open",
            },
        )
    )
    client = _make_client(http_client)
    metadata = await client.get_pull_request(installation_id=1, ref=REF)
    assert metadata.merged is False
