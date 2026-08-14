from __future__ import annotations

from collections.abc import AsyncIterator

import httpx
import pytest
import respx

from patchfrog.domain.pull_request import FileChangeStatus, PullRequestRef
from patchfrog.github.client import GitHubClient
from patchfrog.github.errors import (
    GitHubAuthenticationError,
    GitHubForbiddenError,
    GitHubNotFoundError,
    GitHubRateLimitedError,
    GitHubResponseError,
    GitHubServerError,
    GitHubTimeoutError,
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


PR_RESPONSE = {
    "number": 14,
    "title": "Add ft_strdup",
    "body": "desc",
    "state": "open",
    "html_url": "https://github.com/kadireren7/libft/pull/14",
    "user": {"login": "kadireren7"},
    "base": {"ref": "main", "sha": "aaa"},
    "head": {"ref": "feature", "sha": "bbb"},
}


@respx.mock
async def test_get_pull_request_success(http_client: httpx.AsyncClient) -> None:
    respx.get(f"{API_BASE}/repos/kadireren7/libft/pulls/14").mock(
        return_value=httpx.Response(200, json=PR_RESPONSE)
    )

    client = _make_client(http_client)
    metadata = await client.get_pull_request(installation_id=1, ref=REF)

    assert metadata.number == 14
    assert metadata.author == "kadireren7"
    assert metadata.base_sha == "aaa"
    assert metadata.head_sha == "bbb"


@respx.mock
async def test_list_pull_request_files_success(http_client: httpx.AsyncClient) -> None:
    respx.get(f"{API_BASE}/repos/kadireren7/libft/pulls/14/files").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "filename": "a.c",
                    "status": "added",
                    "additions": 3,
                    "deletions": 0,
                    "patch": "@@ -0,0 +1,3 @@\n+a\n+b\n+c",
                }
            ],
        )
    )

    client = _make_client(http_client)
    files = await client.list_pull_request_files(installation_id=1, ref=REF)

    assert len(files) == 1
    assert files[0].path == "a.c"
    assert files[0].status is FileChangeStatus.ADDED
    assert files[0].additions == 3


@respx.mock
async def test_list_pull_request_files_paginates(http_client: httpx.AsyncClient) -> None:
    route = respx.get(f"{API_BASE}/repos/kadireren7/libft/pulls/14/files")
    full_page = [
        {"filename": f"f{i}.c", "status": "added", "additions": 1, "deletions": 0, "patch": None}
        for i in range(100)
    ]
    partial_page = [
        {"filename": "last.c", "status": "added", "additions": 1, "deletions": 0, "patch": None}
    ]
    route.side_effect = [
        httpx.Response(200, json=full_page),
        httpx.Response(200, json=partial_page),
    ]

    client = _make_client(http_client)
    files = await client.list_pull_request_files(installation_id=1, ref=REF)

    assert len(files) == 101
    assert files[-1].path == "last.c"


@respx.mock
async def test_401_raises_authentication_error(http_client: httpx.AsyncClient) -> None:
    respx.get(f"{API_BASE}/repos/kadireren7/libft/pulls/14").mock(
        return_value=httpx.Response(401, json={"message": "Bad credentials"})
    )
    client = _make_client(http_client)

    with pytest.raises(GitHubAuthenticationError):
        await client.get_pull_request(installation_id=1, ref=REF)


@respx.mock
async def test_403_forbidden_raises_forbidden_error(http_client: httpx.AsyncClient) -> None:
    respx.get(f"{API_BASE}/repos/kadireren7/libft/pulls/14").mock(
        return_value=httpx.Response(403, json={"message": "Forbidden"})
    )
    client = _make_client(http_client)

    with pytest.raises(GitHubForbiddenError):
        await client.get_pull_request(installation_id=1, ref=REF)


@respx.mock
async def test_403_rate_limited_raises_rate_limit_error(http_client: httpx.AsyncClient) -> None:
    respx.get(f"{API_BASE}/repos/kadireren7/libft/pulls/14").mock(
        return_value=httpx.Response(
            403,
            json={"message": "rate limited"},
            headers={"X-RateLimit-Remaining": "0", "Retry-After": "30"},
        )
    )
    client = _make_client(http_client)

    with pytest.raises(GitHubRateLimitedError) as exc_info:
        await client.get_pull_request(installation_id=1, ref=REF)
    assert exc_info.value.retry_after_seconds == 30.0


@respx.mock
async def test_404_raises_not_found_error(http_client: httpx.AsyncClient) -> None:
    respx.get(f"{API_BASE}/repos/kadireren7/libft/pulls/14").mock(
        return_value=httpx.Response(404, json={"message": "Not Found"})
    )
    client = _make_client(http_client)

    with pytest.raises(GitHubNotFoundError):
        await client.get_pull_request(installation_id=1, ref=REF)


@respx.mock
async def test_429_raises_rate_limit_error(http_client: httpx.AsyncClient) -> None:
    respx.get(f"{API_BASE}/repos/kadireren7/libft/pulls/14").mock(
        return_value=httpx.Response(429, json={"message": "too many requests"})
    )
    client = _make_client(http_client)

    with pytest.raises(GitHubRateLimitedError):
        await client.get_pull_request(installation_id=1, ref=REF)


@respx.mock
async def test_500_raises_server_error(http_client: httpx.AsyncClient) -> None:
    respx.get(f"{API_BASE}/repos/kadireren7/libft/pulls/14").mock(
        return_value=httpx.Response(500, text="internal error")
    )
    client = _make_client(http_client)

    with pytest.raises(GitHubServerError):
        await client.get_pull_request(installation_id=1, ref=REF)


@respx.mock
async def test_timeout_raises_timeout_error(http_client: httpx.AsyncClient) -> None:
    respx.get(f"{API_BASE}/repos/kadireren7/libft/pulls/14").mock(
        side_effect=httpx.TimeoutException("timed out")
    )
    client = _make_client(http_client)

    with pytest.raises(GitHubTimeoutError):
        await client.get_pull_request(installation_id=1, ref=REF)


@respx.mock
async def test_invalid_json_raises_response_error(http_client: httpx.AsyncClient) -> None:
    respx.get(f"{API_BASE}/repos/kadireren7/libft/pulls/14").mock(
        return_value=httpx.Response(200, text="not json{{{")
    )
    client = _make_client(http_client)

    with pytest.raises(GitHubResponseError):
        await client.get_pull_request(installation_id=1, ref=REF)


@respx.mock
async def test_malformed_pull_request_shape_raises_response_error(
    http_client: httpx.AsyncClient,
) -> None:
    respx.get(f"{API_BASE}/repos/kadireren7/libft/pulls/14").mock(
        return_value=httpx.Response(200, json={"number": 14})
    )
    client = _make_client(http_client)

    with pytest.raises(GitHubResponseError):
        await client.get_pull_request(installation_id=1, ref=REF)
