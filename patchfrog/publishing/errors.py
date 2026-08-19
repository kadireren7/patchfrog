"""Failure classification for GitHub review-publishing.

Turns the existing :mod:`patchfrog.github.errors` hierarchy into a
retry policy decision -- kept as its own tiny module so both
:mod:`apps.worker.tasks.publish_review` (Celery retry policy) and
:mod:`patchfrog.publishing.service` (persisted failure reason) apply the
exact same classification.
"""

from __future__ import annotations

from enum import StrEnum

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


class PublicationFailureClass(StrEnum):
    """Whether a publish failure is worth retrying, and why not when it
    isn't."""

    RETRYABLE = "retryable"
    RATE_LIMITED = "rate_limited"
    PERMISSION_DENIED = "permission_denied"
    NON_RETRYABLE = "non_retryable"


def classify_github_exception(exc: Exception) -> tuple[PublicationFailureClass, str]:
    """Classify a raised :class:`~patchfrog.github.errors.GitHubError` (or
    anything else) into a retry policy decision plus a clear, loggable
    reason string. Unknown/unexpected exceptions are treated as
    non-retryable by default -- never retry forever against an error we
    don't understand."""

    if isinstance(exc, GitHubRateLimitedError):
        detail = f"rate limited (retry_after_seconds={exc.retry_after_seconds})"
        return PublicationFailureClass.RATE_LIMITED, detail
    if isinstance(exc, (GitHubAuthenticationError, GitHubForbiddenError)):
        return PublicationFailureClass.PERMISSION_DENIED, str(exc)
    if isinstance(exc, (GitHubServerError, GitHubTimeoutError)):
        return PublicationFailureClass.RETRYABLE, str(exc)
    if isinstance(exc, (GitHubUnprocessableError, GitHubNotFoundError, GitHubResponseError, GitHubError)):
        return PublicationFailureClass.NON_RETRYABLE, str(exc)
    return PublicationFailureClass.NON_RETRYABLE, str(exc)
