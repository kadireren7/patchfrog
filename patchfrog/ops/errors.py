"""Operational error taxonomy -- stable failure categories used across the
whole pipeline (ingestion, indexing, static analysis, AI review,
publication), never "review failed."

Generalizes the pattern :mod:`patchfrog.publishing.errors` already
established for the publish stage alone (``PublicationFailureClass`` /
``classify_github_exception``) to every stage. Every category carries an
explicit ``retryable`` verdict, so a Celery task's retry policy is always
driven by *what kind* of failure occurred, never a bare "it raised."
"""

from __future__ import annotations

from enum import StrEnum

from patchfrog.github.errors import (
    GitHubAuthenticationError,
    GitHubForbiddenError,
    GitHubNotFoundError,
    GitHubRateLimitedError,
    GitHubResponseError,
    GitHubServerError,
    GitHubTimeoutError,
    GitHubUnprocessableError,
)
from patchfrog.review.provider import ProviderFatalError, ProviderTransientError


class ErrorCategory(StrEnum):
    """Every stable operational failure category PatchFrog reports.
    Deliberately closed and exhaustive -- an unrecognized exception always
    falls back to ``INTERNAL_ERROR``, never silently disappears."""

    GITHUB_AUTH_ERROR = "github_auth_error"
    GITHUB_RATE_LIMIT = "github_rate_limit"
    REPOSITORY_FETCH_ERROR = "repository_fetch_error"
    INDEXING_ERROR = "indexing_error"
    STATIC_ANALYSIS_ERROR = "static_analysis_error"
    PROVIDER_ERROR = "provider_error"
    PROVIDER_RATE_LIMIT = "provider_rate_limit"
    PROVIDER_TIMEOUT = "provider_timeout"
    VALIDATION_ERROR = "validation_error"
    PUBLICATION_ERROR = "publication_error"
    DATABASE_ERROR = "database_error"
    INTERNAL_ERROR = "internal_error"


def classify_exception(exc: BaseException) -> tuple[ErrorCategory, bool, str]:
    """Classify any exception raised by a pipeline stage into
    ``(category, retryable, detail)``. Never raises; an exception type
    this function doesn't recognize always becomes ``INTERNAL_ERROR,
    retryable=False`` rather than propagating an unclassified failure or
    retrying something unknown forever.

    ``retryable`` is decided per-exception, not purely by category:
    ``GitHubNotFoundError``/``GitHubUnprocessableError`` (a resource that
    doesn't exist, a request GitHub will never accept) are
    ``REPOSITORY_FETCH_ERROR`` but never retryable, while
    ``GitHubServerError``/``GitHubTimeoutError`` are the same category
    and *are* retryable. Likewise ``ProviderFatalError`` (malformed
    request, schema mismatch -- retrying reproduces the identical
    failure) is ``PROVIDER_ERROR`` but never retryable, unlike
    ``ProviderTransientError``.
    """

    if isinstance(exc, GitHubRateLimitedError):
        return (
            ErrorCategory.GITHUB_RATE_LIMIT,
            True,
            f"GitHub rate limited (retry_after_seconds={exc.retry_after_seconds})",
        )
    if isinstance(exc, (GitHubAuthenticationError, GitHubForbiddenError)):
        return ErrorCategory.GITHUB_AUTH_ERROR, False, str(exc)
    if isinstance(exc, (GitHubNotFoundError, GitHubUnprocessableError, GitHubResponseError)):
        return ErrorCategory.REPOSITORY_FETCH_ERROR, False, str(exc)
    if isinstance(exc, (GitHubServerError, GitHubTimeoutError)):
        return ErrorCategory.REPOSITORY_FETCH_ERROR, True, str(exc)
    if isinstance(exc, ProviderTransientError):
        # ProviderTransientError doesn't currently distinguish rate-limit
        # from timeout from a dropped connection at the type level (see
        # patchfrog.review.provider's module docstring) -- classified
        # conservatively as the general retryable PROVIDER_ERROR category
        # rather than fabricating a distinction the exception doesn't
        # actually carry.
        return ErrorCategory.PROVIDER_ERROR, True, str(exc)
    if isinstance(exc, ProviderFatalError):
        return ErrorCategory.PROVIDER_ERROR, False, str(exc)
    if isinstance(exc, (ValueError, TypeError)):
        return ErrorCategory.VALIDATION_ERROR, False, str(exc)

    module = type(exc).__module__
    if module.startswith("sqlalchemy"):
        return ErrorCategory.DATABASE_ERROR, True, str(exc)

    return ErrorCategory.INTERNAL_ERROR, False, str(exc)
