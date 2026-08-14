"""Exception hierarchy for the GitHub boundary.

Nothing outside :mod:`patchfrog.github` should ever need to catch a raw
``httpx`` exception or inspect a raw HTTP status code — everything is
translated into one of these types.
"""

from __future__ import annotations


class GitHubError(Exception):
    """Base class for all GitHub-boundary errors."""


class GitHubAuthenticationError(GitHubError):
    """Authentication with GitHub failed (invalid JWT, expired/invalid installation token)."""


class GitHubNotFoundError(GitHubError):
    """The requested GitHub resource does not exist (HTTP 404)."""


class GitHubForbiddenError(GitHubError):
    """GitHub rejected the request as forbidden (HTTP 403)."""


class GitHubUnprocessableError(GitHubError):
    """GitHub rejected the request as unprocessable (HTTP 422)."""


class GitHubRateLimitedError(GitHubError):
    """GitHub rate-limited the request (HTTP 429, or a 403 rate-limit response)."""

    def __init__(self, message: str, *, retry_after_seconds: float | None = None) -> None:
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds


class GitHubServerError(GitHubError):
    """GitHub returned a server error (HTTP 5xx)."""


class GitHubTimeoutError(GitHubError):
    """The request to GitHub timed out or a network error occurred."""


class GitHubResponseError(GitHubError):
    """GitHub returned a response PatchFrog could not parse or understand."""


class WebhookVerificationError(GitHubError):
    """A webhook payload failed signature verification."""


class WebhookPayloadError(GitHubError):
    """A webhook payload was malformed or missing required fields."""
