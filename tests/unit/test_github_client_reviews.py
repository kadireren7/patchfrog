"""Unit coverage for GitHubClient's review-publishing methods
(create_pull_request_review, list_pull_request_reviews) -- mocked GitHub
API via respx, no live network. Mirrors tests/unit/test_github_client.py's
conventions exactly."""

from __future__ import annotations

from collections.abc import AsyncIterator

import httpx
import pytest
import respx

from patchfrog.domain.github_review import (
    GitHubDiffSide,
    GitHubReviewCommentInput,
    GitHubReviewEvent,
)
from patchfrog.domain.pull_request import PullRequestRef
from patchfrog.github.client import GitHubClient
from patchfrog.github.errors import (
    GitHubAuthenticationError,
    GitHubRateLimitedError,
    GitHubServerError,
    GitHubTimeoutError,
    GitHubUnprocessableError,
)

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


_COMMENT = GitHubReviewCommentInput(path="src/x.py", body="finding body", line=12, side=GitHubDiffSide.RIGHT)


@respx.mock
async def test_create_pull_request_review_success(http_client: httpx.AsyncClient) -> None:
    respx.post(f"{API_BASE}/repos/kadireren7/libft/pulls/14/reviews").mock(
        return_value=httpx.Response(
            200,
            json={"id": 555, "body": "summary", "state": "COMMENTED", "commit_id": "abc123", "user": {"login": "patchfrog-bot"}},
        )
    )
    client = _make_client(http_client)

    result = await client.create_pull_request_review(
        installation_id=1, ref=REF, commit_id="abc123", event=GitHubReviewEvent.COMMENT,
        body="summary", comments=[_COMMENT],
    )

    assert result.id == 555
    assert result.commit_id == "abc123"
    assert result.user_login == "patchfrog-bot"


@respx.mock
async def test_create_pull_request_review_sends_correct_payload(http_client: httpx.AsyncClient) -> None:
    route = respx.post(f"{API_BASE}/repos/kadireren7/libft/pulls/14/reviews").mock(
        return_value=httpx.Response(200, json={"id": 1, "body": "s", "state": "COMMENTED", "commit_id": "sha", "user": None})
    )
    client = _make_client(http_client)

    await client.create_pull_request_review(
        installation_id=1, ref=REF, commit_id="sha", event=GitHubReviewEvent.COMMENT, body="s",
        comments=[GitHubReviewCommentInput(path="a.py", body="b", line=5, side=GitHubDiffSide.RIGHT, start_line=3, start_side=GitHubDiffSide.RIGHT)],
    )

    sent = route.calls.last.request
    import json as _json

    payload = _json.loads(sent.content)
    assert payload["event"] == "COMMENT"
    assert payload["commit_id"] == "sha"
    assert payload["comments"][0]["start_line"] == 3
    assert payload["comments"][0]["side"] == "RIGHT"


@respx.mock
async def test_create_review_never_uses_approve_or_request_changes_event(http_client: httpx.AsyncClient) -> None:
    """Structural guard: GitHubReviewEvent only has COMMENT -- there is no
    way to accidentally submit APPROVE/REQUEST_CHANGES through this
    client (see the enum's own docstring)."""

    assert {e.value for e in GitHubReviewEvent} == {"COMMENT"}


@respx.mock
async def test_create_review_invalid_line_raises_unprocessable(http_client: httpx.AsyncClient) -> None:
    respx.post(f"{API_BASE}/repos/kadireren7/libft/pulls/14/reviews").mock(
        return_value=httpx.Response(422, json={"message": "Validation Failed", "errors": [{"field": "line"}]})
    )
    client = _make_client(http_client)

    with pytest.raises(GitHubUnprocessableError):
        await client.create_pull_request_review(
            installation_id=1, ref=REF, commit_id="sha", event=GitHubReviewEvent.COMMENT, body="s", comments=[_COMMENT]
        )


@respx.mock
async def test_create_review_auth_failure(http_client: httpx.AsyncClient) -> None:
    respx.post(f"{API_BASE}/repos/kadireren7/libft/pulls/14/reviews").mock(return_value=httpx.Response(401))
    client = _make_client(http_client)

    with pytest.raises(GitHubAuthenticationError):
        await client.create_pull_request_review(
            installation_id=1, ref=REF, commit_id="sha", event=GitHubReviewEvent.COMMENT, body="s", comments=[_COMMENT]
        )


@respx.mock
async def test_create_review_rate_limited(http_client: httpx.AsyncClient) -> None:
    respx.post(f"{API_BASE}/repos/kadireren7/libft/pulls/14/reviews").mock(
        return_value=httpx.Response(403, headers={"X-RateLimit-Remaining": "0", "Retry-After": "12"})
    )
    client = _make_client(http_client)

    with pytest.raises(GitHubRateLimitedError) as exc_info:
        await client.create_pull_request_review(
            installation_id=1, ref=REF, commit_id="sha", event=GitHubReviewEvent.COMMENT, body="s", comments=[_COMMENT]
        )
    assert exc_info.value.retry_after_seconds == 12.0


@respx.mock
async def test_create_review_server_error(http_client: httpx.AsyncClient) -> None:
    respx.post(f"{API_BASE}/repos/kadireren7/libft/pulls/14/reviews").mock(return_value=httpx.Response(503))
    client = _make_client(http_client)

    with pytest.raises(GitHubServerError):
        await client.create_pull_request_review(
            installation_id=1, ref=REF, commit_id="sha", event=GitHubReviewEvent.COMMENT, body="s", comments=[_COMMENT]
        )


@respx.mock
async def test_create_review_timeout(http_client: httpx.AsyncClient) -> None:
    respx.post(f"{API_BASE}/repos/kadireren7/libft/pulls/14/reviews").mock(side_effect=httpx.TimeoutException("t/o"))
    client = _make_client(http_client)

    with pytest.raises(GitHubTimeoutError):
        await client.create_pull_request_review(
            installation_id=1, ref=REF, commit_id="sha", event=GitHubReviewEvent.COMMENT, body="s", comments=[_COMMENT]
        )


@respx.mock
async def test_list_pull_request_reviews_finds_patchfrog_marker(http_client: httpx.AsyncClient) -> None:
    respx.get(f"{API_BASE}/repos/kadireren7/libft/pulls/14/reviews").mock(
        return_value=httpx.Response(
            200,
            json=[
                {"id": 1, "body": "unrelated human review", "state": "COMMENTED", "commit_id": "x", "user": {"login": "alice"}},
                {"id": 2, "body": "<!-- patchfrog:review:11111111-1111-1111-1111-111111111111 -->", "state": "COMMENTED", "commit_id": "y", "user": {"login": "patchfrog-bot"}},
            ],
        )
    )
    client = _make_client(http_client)

    reviews = await client.list_pull_request_reviews(installation_id=1, ref=REF)

    assert len(reviews) == 2
    assert reviews[1].id == 2
    assert reviews[1].user_login == "patchfrog-bot"


@respx.mock
async def test_list_pull_request_reviews_paginates(http_client: httpx.AsyncClient) -> None:
    route = respx.get(f"{API_BASE}/repos/kadireren7/libft/pulls/14/reviews")
    full_page = [{"id": i, "body": None, "state": "COMMENTED", "commit_id": "x", "user": None} for i in range(100)]
    partial_page = [{"id": 999, "body": None, "state": "COMMENTED", "commit_id": "x", "user": None}]
    route.side_effect = [httpx.Response(200, json=full_page), httpx.Response(200, json=partial_page)]
    client = _make_client(http_client)

    reviews = await client.list_pull_request_reviews(installation_id=1, ref=REF)

    assert len(reviews) == 101
    assert reviews[-1].id == 999
