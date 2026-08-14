"""Typed GitHub REST API client.

All outbound HTTP calls to the GitHub API go through this module. Callers
work exclusively with internal domain models (:mod:`patchfrog.domain`) —
raw GitHub JSON never leaks past here, and raw ``httpx`` exceptions are
always translated into :mod:`patchfrog.github.errors` types.
"""

from __future__ import annotations

from typing import Any

import httpx

from patchfrog.domain.pull_request import (
    ChangedFile,
    FileChangeStatus,
    PullRequestMetadata,
    PullRequestRef,
)
from patchfrog.github.auth import InstallationTokenProvider
from patchfrog.github.errors import (
    GitHubAuthenticationError,
    GitHubError,
    GitHubForbiddenError,
    GitHubNotFoundError,
    GitHubRateLimitedError,
    GitHubResponseError,
    GitHubServerError,
    GitHubTimeoutError,
    GitHubUnprocessableError,
)

_FILES_PER_PAGE = 100
_MAX_FILES_PAGES = 50  # 5,000 files — far beyond any realistic PR; guards against runaway pagination.


class GitHubClient:
    """Authenticated client for the subset of the GitHub REST API PatchFrog needs."""

    def __init__(
        self,
        *,
        http_client: httpx.AsyncClient,
        token_provider: InstallationTokenProvider,
        api_base_url: str = "https://api.github.com",
        timeout_seconds: float = 10.0,
    ) -> None:
        self._http_client = http_client
        self._token_provider = token_provider
        self._api_base_url = api_base_url.rstrip("/")
        self._timeout = timeout_seconds

    async def get_pull_request(
        self, *, installation_id: int, ref: PullRequestRef
    ) -> PullRequestMetadata:
        """Fetch pull request metadata."""

        path = f"/repos/{ref.owner}/{ref.repository}/pulls/{ref.number}"
        data = await self._get_json(installation_id=installation_id, path=path)
        return _parse_pull_request(data)

    async def list_pull_request_files(
        self, *, installation_id: int, ref: PullRequestRef
    ) -> list[ChangedFile]:
        """Fetch all changed files for a pull request, following pagination."""

        path = f"/repos/{ref.owner}/{ref.repository}/pulls/{ref.number}/files"
        changed_files: list[ChangedFile] = []

        for page in range(1, _MAX_FILES_PAGES + 1):
            data = await self._get_json(
                installation_id=installation_id,
                path=path,
                params={"per_page": _FILES_PER_PAGE, "page": page},
            )
            if not isinstance(data, list):
                raise GitHubResponseError(f"Expected a list of files, got {type(data).__name__}")
            changed_files.extend(_parse_changed_file(item) for item in data)
            if len(data) < _FILES_PER_PAGE:
                break

        return changed_files

    async def _get_json(
        self,
        *,
        installation_id: int,
        path: str,
        params: dict[str, Any] | None = None,
    ) -> Any:
        token = await self._token_provider.get_token(installation_id)
        url = f"{self._api_base_url}{path}"

        try:
            response = await self._http_client.get(
                url,
                params=params,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2022-11-28",
                },
                timeout=self._timeout,
            )
        except httpx.TimeoutException as exc:
            raise GitHubTimeoutError(f"Timed out calling GitHub API: {path}") from exc
        except httpx.HTTPError as exc:
            raise GitHubTimeoutError(f"Network error calling GitHub API: {path}") from exc

        _raise_for_status(response)

        try:
            return response.json()
        except ValueError as exc:
            raise GitHubResponseError(f"GitHub returned malformed JSON for {path}") from exc


def _raise_for_status(response: httpx.Response) -> None:
    status = response.status_code
    if status < 400:
        return

    if status == 401:
        raise GitHubAuthenticationError(f"GitHub authentication failed (status={status})")
    if status == 403:
        if response.headers.get("X-RateLimit-Remaining") == "0":
            raise GitHubRateLimitedError(
                "GitHub primary rate limit exceeded",
                retry_after_seconds=_retry_after(response),
            )
        raise GitHubForbiddenError(f"GitHub forbade the request (status={status})")
    if status == 404:
        raise GitHubNotFoundError(f"GitHub resource not found (status={status})")
    if status == 422:
        raise GitHubUnprocessableError(f"GitHub rejected the request as unprocessable (status={status})")
    if status == 429:
        raise GitHubRateLimitedError(
            "GitHub secondary rate limit exceeded",
            retry_after_seconds=_retry_after(response),
        )
    if status >= 500:
        raise GitHubServerError(f"GitHub server error (status={status})")

    raise GitHubError(f"Unexpected GitHub response status: {status}")


def _retry_after(response: httpx.Response) -> float | None:
    value = response.headers.get("Retry-After")
    if value is None:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _parse_pull_request(data: dict[str, Any]) -> PullRequestMetadata:
    try:
        return PullRequestMetadata(
            number=data["number"],
            title=data["title"],
            body=data.get("body"),
            author=data["user"]["login"],
            base_branch=data["base"]["ref"],
            head_branch=data["head"]["ref"],
            base_sha=data["base"]["sha"],
            head_sha=data["head"]["sha"],
            html_url=data["html_url"],
            state=data["state"],
        )
    except (KeyError, TypeError) as exc:
        raise GitHubResponseError("Malformed pull request response from GitHub") from exc


def _parse_changed_file(data: dict[str, Any]) -> ChangedFile:
    try:
        return ChangedFile(
            path=data["filename"],
            previous_path=data.get("previous_filename"),
            status=FileChangeStatus(data["status"]),
            additions=data["additions"],
            deletions=data["deletions"],
            patch=data.get("patch"),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise GitHubResponseError("Malformed changed-file response from GitHub") from exc
