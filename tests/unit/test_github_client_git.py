"""M9.2/M9.6: the new Git Data API + pull-request-creation surface on
:class:`GitHubClient` -- branch/commit/tree creation and PR open/list/
update. Every call is intercepted by ``respx``; no real GitHub request is
ever made."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator

import httpx
import pytest
import respx

from patchfrog.domain.github_git import GitTreeEntry
from patchfrog.github.client import GitHubClient
from patchfrog.github.errors import GitHubResponseError, GitHubUnprocessableError

API_BASE = "https://api.github.com"


class _TokenProvider:
    async def get_token(self, installation_id: int) -> str:
        return "token"


@pytest.fixture
async def http_client() -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient() as client:
        yield client


def _client(http_client: httpx.AsyncClient) -> GitHubClient:
    return GitHubClient(http_client=http_client, token_provider=_TokenProvider(), api_base_url=API_BASE)  # type: ignore[arg-type]


@respx.mock
async def test_get_ref_returns_sha(http_client: httpx.AsyncClient) -> None:
    respx.get(f"{API_BASE}/repos/octo/repo/git/ref/heads/main").mock(
        return_value=httpx.Response(200, json={"object": {"sha": "a" * 40}})
    )
    sha = await _client(http_client).get_ref(installation_id=1, owner="octo", repository="repo", ref="heads/main")
    assert sha == "a" * 40


@respx.mock
async def test_get_ref_returns_none_when_missing(http_client: httpx.AsyncClient) -> None:
    respx.get(f"{API_BASE}/repos/octo/repo/git/ref/heads/gone").mock(return_value=httpx.Response(404, json={}))
    sha = await _client(http_client).get_ref(installation_id=1, owner="octo", repository="repo", ref="heads/gone")
    assert sha is None


@respx.mock
async def test_create_ref_posts_full_ref_and_sha(http_client: httpx.AsyncClient) -> None:
    route = respx.post(f"{API_BASE}/repos/octo/repo/git/refs").mock(
        return_value=httpx.Response(201, json={"ref": "refs/heads/x", "object": {"sha": "b" * 40}})
    )
    await _client(http_client).create_ref(
        installation_id=1, owner="octo", repository="repo", ref="refs/heads/patchfrog/migrate/acme/abc123", sha="b" * 40,
    )
    payload = json.loads(route.calls.last.request.content)
    assert payload == {"ref": "refs/heads/patchfrog/migrate/acme/abc123", "sha": "b" * 40}


@respx.mock
async def test_create_ref_collision_raises_unprocessable(http_client: httpx.AsyncClient) -> None:
    respx.post(f"{API_BASE}/repos/octo/repo/git/refs").mock(
        return_value=httpx.Response(422, json={"message": "Reference already exists"})
    )
    with pytest.raises(GitHubUnprocessableError):
        await _client(http_client).create_ref(
            installation_id=1, owner="octo", repository="repo", ref="refs/heads/x", sha="c" * 40,
        )


@respx.mock
async def test_update_ref_patches_sha_and_force(http_client: httpx.AsyncClient) -> None:
    route = respx.patch(f"{API_BASE}/repos/octo/repo/git/refs/heads/x").mock(
        return_value=httpx.Response(200, json={"ref": "refs/heads/x", "object": {"sha": "d" * 40}})
    )
    await _client(http_client).update_ref(
        installation_id=1, owner="octo", repository="repo", ref="heads/x", sha="d" * 40, force=True,
    )
    payload = json.loads(route.calls.last.request.content)
    assert payload == {"sha": "d" * 40, "force": True}


@respx.mock
async def test_get_commit_tree_sha(http_client: httpx.AsyncClient) -> None:
    respx.get(f"{API_BASE}/repos/octo/repo/git/commits/{'e' * 40}").mock(
        return_value=httpx.Response(200, json={"tree": {"sha": "f" * 40}})
    )
    tree_sha = await _client(http_client).get_commit_tree_sha(
        installation_id=1, owner="octo", repository="repo", commit_sha="e" * 40,
    )
    assert tree_sha == "f" * 40


@respx.mock
async def test_create_tree_sends_file_content_entries(http_client: httpx.AsyncClient) -> None:
    route = respx.post(f"{API_BASE}/repos/octo/repo/git/trees").mock(
        return_value=httpx.Response(201, json={"sha": "g" * 40})
    )
    tree_sha = await _client(http_client).create_tree(
        installation_id=1, owner="octo", repository="repo", base_tree_sha="h" * 40,
        entries=[GitTreeEntry(path="app/chat.py", content="new content")],
    )
    assert tree_sha == "g" * 40
    payload = json.loads(route.calls.last.request.content)
    assert payload["base_tree"] == "h" * 40
    assert payload["tree"] == [{"path": "app/chat.py", "mode": "100644", "type": "blob", "content": "new content"}]


@respx.mock
async def test_create_commit_sends_message_tree_and_parent(http_client: httpx.AsyncClient) -> None:
    route = respx.post(f"{API_BASE}/repos/octo/repo/git/commits").mock(
        return_value=httpx.Response(201, json={"sha": "i" * 40})
    )
    commit_sha = await _client(http_client).create_commit(
        installation_id=1, owner="octo", repository="repo", message="migrate acme-ai", tree_sha="j" * 40,
        parent_sha="k" * 40,
    )
    assert commit_sha == "i" * 40
    payload = json.loads(route.calls.last.request.content)
    assert payload == {"message": "migrate acme-ai", "tree": "j" * 40, "parents": ["k" * 40]}


@respx.mock
async def test_create_pull_request_posts_title_body_head_base(http_client: httpx.AsyncClient) -> None:
    response = {
        "number": 42, "title": "Migrate acme-ai", "body": "dossier", "user": {"login": "patchfrog[bot]"},
        "base": {"ref": "main", "sha": "l" * 40}, "head": {"ref": "patchfrog/migrate/acme/abc", "sha": "m" * 40},
        "html_url": "https://github.com/octo/repo/pull/42", "state": "open", "merged": False,
    }
    route = respx.post(f"{API_BASE}/repos/octo/repo/pulls").mock(return_value=httpx.Response(201, json=response))
    pr = await _client(http_client).create_pull_request(
        installation_id=1, owner="octo", repository="repo", title="Migrate acme-ai", body="dossier",
        head="patchfrog/migrate/acme/abc", base="main",
    )
    assert pr.number == 42 and pr.html_url == "https://github.com/octo/repo/pull/42"
    payload = json.loads(route.calls.last.request.content)
    assert payload == {"title": "Migrate acme-ai", "body": "dossier", "head": "patchfrog/migrate/acme/abc", "base": "main"}


@respx.mock
async def test_list_pull_requests_filters_by_head(http_client: httpx.AsyncClient) -> None:
    response = [
        {
            "number": 7, "title": "t", "body": None, "user": {"login": "u"}, "base": {"ref": "main", "sha": "a" * 40},
            "head": {"ref": "patchfrog/migrate/acme/abc", "sha": "b" * 40},
            "html_url": "https://github.com/octo/repo/pull/7", "state": "open", "merged": False,
        }
    ]
    route = respx.get(f"{API_BASE}/repos/octo/repo/pulls").mock(return_value=httpx.Response(200, json=response))
    pull_requests = await _client(http_client).list_pull_requests(
        installation_id=1, owner="octo", repository="repo", head="octo:patchfrog/migrate/acme/abc",
    )
    assert [pr.number for pr in pull_requests] == [7]
    assert route.calls.last.request.url.params["head"] == "octo:patchfrog/migrate/acme/abc"


@respx.mock
async def test_update_pull_request_patches_only_given_fields(http_client: httpx.AsyncClient) -> None:
    response = {
        "number": 7, "title": "new title", "body": "old body", "user": {"login": "u"},
        "base": {"ref": "main", "sha": "a" * 40}, "head": {"ref": "x", "sha": "b" * 40},
        "html_url": "https://github.com/octo/repo/pull/7", "state": "open", "merged": False,
    }
    route = respx.patch(f"{API_BASE}/repos/octo/repo/pulls/7").mock(return_value=httpx.Response(200, json=response))
    pr = await _client(http_client).update_pull_request(
        installation_id=1, owner="octo", repository="repo", number=7, title="new title",
    )
    assert pr.title == "new title"
    payload = json.loads(route.calls.last.request.content)
    assert payload == {"title": "new title"}


@respx.mock
async def test_malformed_ref_response_raises(http_client: httpx.AsyncClient) -> None:
    respx.get(f"{API_BASE}/repos/octo/repo/git/ref/heads/main").mock(return_value=httpx.Response(200, json={}))
    with pytest.raises(GitHubResponseError):
        await _client(http_client).get_ref(installation_id=1, owner="octo", repository="repo", ref="heads/main")


@respx.mock
async def test_create_pull_request_can_open_a_draft(http_client: httpx.AsyncClient) -> None:
    response = {
        "number": 43, "title": "t", "body": "b", "user": {"login": "patchfrog[bot]"},
        "base": {"ref": "main", "sha": "l" * 40}, "head": {"ref": "x", "sha": "m" * 40},
        "html_url": "https://github.com/octo/repo/pull/43", "state": "open", "merged": False,
    }
    route = respx.post(f"{API_BASE}/repos/octo/repo/pulls").mock(return_value=httpx.Response(201, json=response))
    await _client(http_client).create_pull_request(
        installation_id=1, owner="octo", repository="repo", title="t", body="b", head="x", base="main", draft=True,
    )
    assert json.loads(route.calls.last.request.content)["draft"] is True


@respx.mock
async def test_get_default_branch_returns_name_and_head_together(http_client: httpx.AsyncClient) -> None:
    respx.get(f"{API_BASE}/repos/octo/repo").mock(return_value=httpx.Response(200, json={"default_branch": "trunk"}))
    respx.get(f"{API_BASE}/repos/octo/repo/branches/trunk").mock(
        return_value=httpx.Response(200, json={"commit": {"sha": "c" * 40}})
    )
    client = _client(http_client)
    assert await client.get_default_branch(installation_id=1, owner="octo", repository="repo") == ("trunk", "c" * 40)
    assert await client.get_default_branch_head_sha(installation_id=1, owner="octo", repository="repo") == "c" * 40


@respx.mock
async def test_get_default_branch_rejects_a_response_without_a_branch(http_client: httpx.AsyncClient) -> None:
    respx.get(f"{API_BASE}/repos/octo/repo").mock(return_value=httpx.Response(200, json={}))
    with pytest.raises(GitHubResponseError):
        await _client(http_client).get_default_branch(installation_id=1, owner="octo", repository="repo")
