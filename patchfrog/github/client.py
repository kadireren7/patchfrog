"""Typed GitHub REST API client.

All outbound HTTP calls to the GitHub API go through this module. Callers
work exclusively with internal domain models (:mod:`patchfrog.domain`) —
raw GitHub JSON never leaks past here, and raw ``httpx`` exceptions are
always translated into :mod:`patchfrog.github.errors` types.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import httpx

from patchfrog.domain.github_feedback import (
    GitHubActor,
    GitHubActorType,
    GitHubReaction,
    GitHubReactionContent,
    GitHubReviewComment,
    GitHubReviewThreadStatus,
)
from patchfrog.domain.github_review import (
    GitHubReviewCommentInput,
    GitHubReviewEvent,
    GitHubSubmittedReview,
)
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
_REVIEWS_PER_PAGE = 100
_MAX_REVIEWS_PAGES = 20  # 2,000 reviews — far beyond any realistic PR.
_COMMENTS_PER_PAGE = 100
_MAX_COMMENTS_PAGES = 50  # 5,000 review comments — far beyond any realistic PR.
_REACTIONS_PER_PAGE = 100
_MAX_REACTIONS_PAGES = 10  # 1,000 reactions on one comment — far beyond realistic use.


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

    async def get_default_branch_head_sha(
        self, *, installation_id: int, owner: str, repository: str
    ) -> str:
        """The exact commit SHA at the tip of the repository's default
        branch, right now -- used by ``patchfrog ops preflight`` (external
        beta readiness) to resolve ``.patchfrog.yml`` for a repository
        that has no open PR yet. Never used by the real review/publish
        pipeline itself (that always works from a webhook-supplied
        ``head_sha``, never a freshly-queried default branch -- see the
        module docstring of :mod:`patchfrog.publishing.config_resolution`)."""

        repo_data = await self._get_json(installation_id=installation_id, path=f"/repos/{owner}/{repository}")
        default_branch = repo_data.get("default_branch")
        if not isinstance(default_branch, str) or not default_branch:
            raise GitHubResponseError(f"GitHub did not report a default_branch for {owner}/{repository}")

        branch_data = await self._get_json(
            installation_id=installation_id, path=f"/repos/{owner}/{repository}/branches/{default_branch}"
        )
        sha = branch_data.get("commit", {}).get("sha")
        if not isinstance(sha, str) or not sha:
            raise GitHubResponseError(f"GitHub did not report a head commit for {owner}/{repository}@{default_branch}")
        return sha

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

    async def list_pull_request_reviews(
        self, *, installation_id: int, ref: PullRequestRef
    ) -> list[GitHubSubmittedReview]:
        """Fetch all reviews already submitted on a pull request, following
        pagination. Used for reconciliation -- discovering a review
        PatchFrog previously published (by its marker) when local
        persistence state is uncertain (crash mid-publish, retry)."""

        path = f"/repos/{ref.owner}/{ref.repository}/pulls/{ref.number}/reviews"
        reviews: list[GitHubSubmittedReview] = []

        for page in range(1, _MAX_REVIEWS_PAGES + 1):
            data = await self._get_json(
                installation_id=installation_id,
                path=path,
                params={"per_page": _REVIEWS_PER_PAGE, "page": page},
            )
            if not isinstance(data, list):
                raise GitHubResponseError(f"Expected a list of reviews, got {type(data).__name__}")
            reviews.extend(_parse_submitted_review(item) for item in data)
            if len(data) < _REVIEWS_PER_PAGE:
                break

        return reviews

    async def create_pull_request_review(
        self,
        *,
        installation_id: int,
        ref: PullRequestRef,
        commit_id: str,
        event: GitHubReviewEvent,
        body: str,
        comments: list[GitHubReviewCommentInput],
    ) -> GitHubSubmittedReview:
        """Submit one pull request review, optionally with inline comments.

        ``commit_id`` pins the review to a specific commit -- GitHub
        rejects (422) a review whose ``commit_id`` is not the PR's current
        head, which is itself a second, server-side layer of the
        stale-head protection :mod:`patchfrog.publishing` enforces before
        ever reaching this call (see
        :mod:`patchfrog.publishing.service`)."""

        path = f"/repos/{ref.owner}/{ref.repository}/pulls/{ref.number}/reviews"
        payload: dict[str, Any] = {
            "commit_id": commit_id,
            "body": body,
            "event": event.value,
            "comments": [_serialize_comment(c) for c in comments],
        }
        data = await self._post_json(installation_id=installation_id, path=path, json_body=payload)
        return _parse_submitted_review(data)

    async def list_pull_request_review_comments(
        self, *, installation_id: int, ref: PullRequestRef
    ) -> list[GitHubReviewComment]:
        """Fetch every review (inline) comment on a pull request, following
        pagination -- covers PatchFrog's own published comments and any
        developer reply to them (``in_reply_to_id`` set). Used by
        :mod:`patchfrog.feedback.sync` for both reply ingestion and
        ``github_comment_id`` enrichment (Phase 9); read-only, requires
        no permission beyond the existing ``pull_requests`` grant."""

        path = f"/repos/{ref.owner}/{ref.repository}/pulls/{ref.number}/comments"
        comments: list[GitHubReviewComment] = []

        for page in range(1, _MAX_COMMENTS_PAGES + 1):
            data = await self._get_json(
                installation_id=installation_id,
                path=path,
                params={"per_page": _COMMENTS_PER_PAGE, "page": page},
            )
            if not isinstance(data, list):
                raise GitHubResponseError(f"Expected a list of review comments, got {type(data).__name__}")
            comments.extend(_parse_review_comment(item) for item in data)
            if len(data) < _COMMENTS_PER_PAGE:
                break

        return comments

    async def list_review_comment_reactions(
        self, *, installation_id: int, ref: PullRequestRef, comment_id: int
    ) -> list[GitHubReaction]:
        """Fetch every reaction on one review comment, following
        pagination. GitHub has no webhook event for reaction add/remove --
        this is always a polled/synced signal (see
        :mod:`patchfrog.feedback.sync`'s module docstring)."""

        path = f"/repos/{ref.owner}/{ref.repository}/pulls/comments/{comment_id}/reactions"
        reactions: list[GitHubReaction] = []

        for page in range(1, _MAX_REACTIONS_PAGES + 1):
            data = await self._get_json(
                installation_id=installation_id,
                path=path,
                params={"per_page": _REACTIONS_PER_PAGE, "page": page},
            )
            if not isinstance(data, list):
                raise GitHubResponseError(f"Expected a list of reactions, got {type(data).__name__}")
            reactions.extend(_parse_reaction(item) for item in data if _is_known_reaction(item))
            if len(data) < _REACTIONS_PER_PAGE:
                break

        return reactions

    async def list_review_thread_statuses(
        self, *, installation_id: int, ref: PullRequestRef
    ) -> list[GitHubReviewThreadStatus]:
        """Fetch every review thread's resolution state via GraphQL --
        REST has no field for this at all. Uses the same installation
        token / permission scope as every REST call (GraphQL access is
        governed by the same App permissions, never a separate grant).
        Read-only; never mutates thread state."""

        query = """
        query($owner: String!, $repo: String!, $number: Int!, $after: String) {
          repository(owner: $owner, name: $repo) {
            pullRequest(number: $number) {
              reviewThreads(first: 100, after: $after) {
                pageInfo { hasNextPage endCursor }
                nodes {
                  isResolved
                  comments(first: 1) { nodes { databaseId } }
                }
              }
            }
          }
        }
        """
        statuses: list[GitHubReviewThreadStatus] = []
        after: str | None = None
        for _ in range(_MAX_REVIEWS_PAGES):
            variables = {"owner": ref.owner, "repo": ref.repository, "number": ref.number, "after": after}
            data = await self._post_graphql(installation_id=installation_id, query=query, variables=variables)
            try:
                connection = data["data"]["repository"]["pullRequest"]["reviewThreads"]
                for node in connection["nodes"]:
                    comment_nodes = node["comments"]["nodes"]
                    first_comment_id = comment_nodes[0]["databaseId"] if comment_nodes else None
                    statuses.append(
                        GitHubReviewThreadStatus(first_comment_id=first_comment_id, is_resolved=bool(node["isResolved"]))
                    )
                page_info = connection["pageInfo"]
            except (KeyError, TypeError, IndexError) as exc:
                raise GitHubResponseError("Malformed reviewThreads GraphQL response") from exc
            if not page_info.get("hasNextPage"):
                break
            after = page_info.get("endCursor")

        return statuses

    async def _post_graphql(
        self, *, installation_id: int, query: str, variables: dict[str, Any]
    ) -> dict[str, Any]:
        token = await self._token_provider.get_token(installation_id)
        url = f"{self._api_base_url}/graphql"

        try:
            response = await self._http_client.post(
                url,
                json={"query": query, "variables": variables},
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/vnd.github+json",
                },
                timeout=self._timeout,
            )
        except httpx.TimeoutException as exc:
            raise GitHubTimeoutError("Timed out calling GitHub GraphQL API") from exc
        except httpx.HTTPError as exc:
            raise GitHubTimeoutError("Network error calling GitHub GraphQL API") from exc

        _raise_for_status(response)

        try:
            data: dict[str, Any] = response.json()
        except ValueError as exc:
            raise GitHubResponseError("GitHub GraphQL API returned malformed JSON") from exc

        if data.get("errors"):
            raise GitHubResponseError(f"GitHub GraphQL API returned errors: {data['errors']}")

        return data

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

    async def _post_json(
        self,
        *,
        installation_id: int,
        path: str,
        json_body: dict[str, Any],
    ) -> Any:
        token = await self._token_provider.get_token(installation_id)
        url = f"{self._api_base_url}{path}"

        try:
            response = await self._http_client.post(
                url,
                json=json_body,
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
            merged=bool(data.get("merged", False)),
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


def _parse_submitted_review(data: dict[str, Any]) -> GitHubSubmittedReview:
    try:
        return GitHubSubmittedReview(
            id=data["id"],
            body=data.get("body"),
            state=data["state"],
            commit_id=data.get("commit_id"),
            user_login=(data.get("user") or {}).get("login"),
        )
    except (KeyError, TypeError) as exc:
        raise GitHubResponseError("Malformed review response from GitHub") from exc


def _parse_actor(data: dict[str, Any] | None) -> GitHubActor:
    if not data:
        return GitHubActor(login="", actor_type=GitHubActorType.USER)
    raw_type = data.get("type", "User")
    try:
        actor_type = GitHubActorType(raw_type)
    except ValueError:
        # An actor type GitHub added after this enum was written -- never
        # fail parsing over it; treat as a non-bot User conservatively
        # (see the module docstring: unknown never silently becomes Bot,
        # which would wrongly discard real developer feedback).
        actor_type = GitHubActorType.USER
    return GitHubActor(login=str(data.get("login", "")), actor_type=actor_type)


def _parse_github_datetime(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _parse_review_comment(data: dict[str, Any]) -> GitHubReviewComment:
    try:
        return GitHubReviewComment(
            id=data["id"],
            path=data["path"],
            line=data.get("line"),
            original_line=data.get("original_line"),
            side=data.get("side"),
            body=data.get("body", ""),
            actor=_parse_actor(data.get("user")),
            in_reply_to_id=data.get("in_reply_to_id"),
            pull_request_review_id=data.get("pull_request_review_id"),
            created_at=_parse_github_datetime(data["created_at"]),
            updated_at=_parse_github_datetime(data["updated_at"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise GitHubResponseError("Malformed review comment response from GitHub") from exc


def _is_known_reaction(data: dict[str, Any]) -> bool:
    return data.get("content") in {c.value for c in GitHubReactionContent}


def _parse_reaction(data: dict[str, Any]) -> GitHubReaction:
    try:
        return GitHubReaction(
            id=data["id"],
            content=GitHubReactionContent(data["content"]),
            actor=_parse_actor(data.get("user")),
            created_at=_parse_github_datetime(data["created_at"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise GitHubResponseError("Malformed reaction response from GitHub") from exc


def _serialize_comment(comment: GitHubReviewCommentInput) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "path": comment.path,
        "body": comment.body,
        "line": comment.line,
        "side": comment.side.value,
    }
    if comment.start_line is not None:
        payload["start_line"] = comment.start_line
    if comment.start_side is not None:
        payload["start_side"] = comment.start_side.value
    return payload
