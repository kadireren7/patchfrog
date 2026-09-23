from __future__ import annotations

import json
from collections.abc import AsyncIterator

import httpx
import pytest
import respx

from patchfrog.domain.github_check import (
    GitHubCheckConclusion,
    GitHubCheckOutput,
    GitHubCheckRunInput,
    GitHubCheckStatus,
)
from patchfrog.domain.pull_request import PullRequestRef
from patchfrog.github.client import GitHubClient

API_BASE = "https://api.github.com"
REF = PullRequestRef(owner="octo", repository="repo", number=3)


class _TokenProvider:
    async def get_token(self, installation_id: int) -> str:
        return "token"


@pytest.fixture
async def http_client() -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient() as client:
        yield client


def _client(http_client: httpx.AsyncClient) -> GitHubClient:
    return GitHubClient(
        http_client=http_client,
        token_provider=_TokenProvider(),  # type: ignore[arg-type]
        api_base_url=API_BASE,
    )


def _input(status: GitHubCheckStatus, conclusion: GitHubCheckConclusion | None = None) -> GitHubCheckRunInput:
    return GitHubCheckRunInput(
        name="PatchFrog review",
        head_sha="a" * 40,
        external_id="stable-id",
        status=status,
        conclusion=conclusion,
        output=GitHubCheckOutput(title="title", summary="summary"),
    )


@respx.mock
async def test_create_and_update_check_run_use_checks_api(http_client: httpx.AsyncClient) -> None:
    response = {
        "id": 91,
        "name": "PatchFrog review",
        "head_sha": "a" * 40,
        "external_id": "stable-id",
        "status": "completed",
        "conclusion": "success",
    }
    create = respx.post(f"{API_BASE}/repos/octo/repo/check-runs").mock(
        return_value=httpx.Response(201, json=response)
    )
    update = respx.patch(f"{API_BASE}/repos/octo/repo/check-runs/91").mock(
        return_value=httpx.Response(200, json=response)
    )
    client = _client(http_client)
    check = _input(GitHubCheckStatus.COMPLETED, GitHubCheckConclusion.SUCCESS)

    await client.create_check_run(installation_id=5, ref=REF, check=check)
    await client.update_check_run(installation_id=5, ref=REF, check_run_id=91, check=check)

    create_payload = json.loads(create.calls.last.request.content)
    update_payload = json.loads(update.calls.last.request.content)
    assert create_payload["head_sha"] == "a" * 40
    assert create_payload["conclusion"] == "success"
    assert "head_sha" not in update_payload


@respx.mock
async def test_list_check_runs_preserves_external_identity(http_client: httpx.AsyncClient) -> None:
    respx.get(f"{API_BASE}/repos/octo/repo/commits/{'a' * 40}/check-runs").mock(
        return_value=httpx.Response(
            200,
            json={
                "total_count": 1,
                "check_runs": [
                    {
                        "id": 91,
                        "name": "PatchFrog review",
                        "head_sha": "a" * 40,
                        "external_id": "stable-id",
                        "status": "in_progress",
                        "conclusion": None,
                    }
                ],
            },
        )
    )
    checks = await _client(http_client).list_check_runs(
        installation_id=5,
        ref=REF,
        head_sha="a" * 40,
    )
    assert checks[0].external_id == "stable-id"
    assert checks[0].status is GitHubCheckStatus.IN_PROGRESS
