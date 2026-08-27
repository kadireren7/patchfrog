"""Unit coverage for :mod:`patchfrog.ops.errors` -- the operational error
taxonomy. Every case here checks both the category *and* the retryable
verdict, since a single category can be raised by both a retryable and a
non-retryable exception (e.g. REPOSITORY_FETCH_ERROR, PROVIDER_ERROR)."""

from __future__ import annotations

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
from patchfrog.ops.errors import ErrorCategory, classify_exception
from patchfrog.review.provider import ProviderFatalError, ProviderTransientError
from patchfrog.review.provider_factory import MissingProviderCredentialsError


def test_rate_limited_is_retryable() -> None:
    category, retryable, _detail = classify_exception(GitHubRateLimitedError("nope", retry_after_seconds=30))
    assert category is ErrorCategory.GITHUB_RATE_LIMIT
    assert retryable is True


def test_auth_errors_are_never_retryable() -> None:
    for exc in (GitHubAuthenticationError("x"), GitHubForbiddenError("x")):
        category, retryable, _ = classify_exception(exc)
        assert category is ErrorCategory.GITHUB_AUTH_ERROR
        assert retryable is False


def test_not_found_and_unprocessable_are_repository_fetch_error_but_not_retryable() -> None:
    for exc in (GitHubNotFoundError("x"), GitHubUnprocessableError("x"), GitHubResponseError("x")):
        category, retryable, _ = classify_exception(exc)
        assert category is ErrorCategory.REPOSITORY_FETCH_ERROR
        assert retryable is False


def test_server_error_and_timeout_are_repository_fetch_error_and_retryable() -> None:
    for exc in (GitHubServerError("x"), GitHubTimeoutError("x")):
        category, retryable, _ = classify_exception(exc)
        assert category is ErrorCategory.REPOSITORY_FETCH_ERROR
        assert retryable is True


def test_provider_transient_is_retryable() -> None:
    category, retryable, _ = classify_exception(ProviderTransientError("rate limited"))
    assert category is ErrorCategory.PROVIDER_ERROR
    assert retryable is True


def test_provider_fatal_is_never_retryable_despite_same_category() -> None:
    """Regression test: the first version of this classifier collapsed
    ProviderTransientError and ProviderFatalError into one retryable
    category, which would have caused Celery to retry a fatal,
    reproduce-identically failure forever."""

    category, retryable, _ = classify_exception(ProviderFatalError("malformed request"))
    assert category is ErrorCategory.PROVIDER_ERROR
    assert retryable is False


def test_value_and_type_errors_are_validation_errors_never_retryable() -> None:
    for exc in (ValueError("bad"), TypeError("bad")):
        category, retryable, _ = classify_exception(exc)
        assert category is ErrorCategory.VALIDATION_ERROR
        assert retryable is False


def test_missing_provider_credentials_is_provider_error_never_retryable() -> None:
    """Regression test: found live during the private beta validation
    sprint's real GitHub dogfood -- a real webhook-triggered review on a
    real PR hit this exact exception (no ANTHROPIC_API_KEY in the
    environment) and it fell through to the generic INTERNAL_ERROR
    catch-all, making a routine, actionable deployment-configuration
    problem indistinguishable from an unexpected PatchFrog bug in
    `patchfrog_reviews_failed_total`/`patchfrog ops failed`."""

    category, retryable, _ = classify_exception(MissingProviderCredentialsError("ANTHROPIC_API_KEY is not set"))
    assert category is ErrorCategory.PROVIDER_ERROR
    assert retryable is False


def test_unrecognized_exception_is_internal_error_never_retryable() -> None:
    class _WeirdError(Exception):
        pass

    category, retryable, _ = classify_exception(_WeirdError("mystery"))
    assert category is ErrorCategory.INTERNAL_ERROR
    assert retryable is False
