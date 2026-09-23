"""Provider-neutral LLM abstraction.

Every real or fake LLM backend implements :class:`LLMProvider` -- a single
narrow method, ``generate_structured``, that takes a system prompt, a user
prompt, and a JSON Schema, and returns raw structured JSON text plus usage
and latency. Nothing above this boundary (candidate selection, prompt
building, validation, the critic, persistence) knows or cares which
concrete provider is behind it -- that's the entire point of the
abstraction, and it's what makes :class:`~patchfrog.review.providers.fake.FakeLLMProvider`
a legitimate stand-in for tests rather than a mock of internal plumbing.

The LLM is never given tools, shell access, database access, or network
access here -- ``generate_structured`` is a single request/response call.
Any "action" the model proposes (a suggested fix, a finding) is text in a
structured response that PatchFrog validates before trusting; the model
itself never touches GitHub, the database, or the filesystem.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol


@dataclass(frozen=True, slots=True)
class ProviderRequest:
    """One structured-output request to an LLM provider."""

    system_prompt: str
    user_prompt: str
    json_schema: dict[str, Any]
    schema_name: str
    max_output_tokens: int


@dataclass(frozen=True, slots=True)
class ProviderUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    #: Reasoning/"thinking" tokens, when a provider bills and reports them
    #: as a distinct line item from ``output_tokens`` (e.g. Gemini's
    #: ``thoughts_token_count``). Always 0 for a provider that doesn't
    #: expose this separately (e.g. Anthropic folds extended-thinking
    #: tokens into ``output_tokens``) -- never fabricated when a provider
    #: doesn't report it.
    thinking_tokens: int = 0


@dataclass(frozen=True, slots=True)
class ProviderResult:
    """The provider's raw structured response -- not yet parsed into a
    domain model, and not yet validated. ``raw_json`` is the exact text
    the provider returned; callers parse it themselves so a
    schema-violation failure mode can be tested without needing a second
    provider-shaped error for it."""

    raw_json: str
    usage: ProviderUsage
    latency_ms: float
    stop_reason: str | None = None


class ProviderError(Exception):
    """Base class for every provider failure.

    ``retry_after_seconds`` (default ``None``) is an optional, provider-
    reported hint for how long to wait before trying again -- Gemini's
    ``google.rpc.RetryInfo.retryDelay`` or an HTTP ``Retry-After`` header
    (Anthropic/OpenAI). When present, :func:`patchfrog.review.retry.call_with_retry`
    honors it instead of its own exponential backoff, since the provider
    itself is the authority on how long its rate limit lasts. Always
    ``None`` for a fatal error (never retried, so irrelevant) and for a
    transient error whose provider didn't report a delay -- exponential
    backoff remains the fallback in that case.
    """

    def __init__(self, message: str, *, retry_after_seconds: float | None = None) -> None:
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds


class ProviderFailureKind(StrEnum):
    INSUFFICIENT_QUOTA = "insufficient_quota"
    AUTHENTICATION = "authentication_failure"
    INVALID_MODEL = "invalid_model"
    RATE_LIMIT = "rate_limit"
    TRANSIENT_SERVER = "transient_server_error"
    TIMEOUT = "timeout"
    INVALID_REQUEST = "invalid_request"
    REFUSAL = "refusal"
    UNKNOWN = "unknown"


class ProviderTransientError(ProviderError):
    """A failure that is safe to retry with bounded backoff: rate limits,
    server-side overload, or a dropped connection. Never raised for
    anything that would repeat identically on retry."""

    kind: ProviderFailureKind = ProviderFailureKind.TRANSIENT_SERVER


class ProviderFatalError(ProviderError):
    """A failure that must never be retried: an auth failure, a malformed
    request (HTTP 400), or a response that doesn't parse against the
    requested schema. Retrying would just repeat the same failure."""

    kind: ProviderFailureKind = ProviderFailureKind.UNKNOWN


class ProviderInsufficientQuotaError(ProviderFatalError):
    kind = ProviderFailureKind.INSUFFICIENT_QUOTA


class ProviderAuthenticationError(ProviderFatalError):
    kind = ProviderFailureKind.AUTHENTICATION


class ProviderInvalidModelError(ProviderFatalError):
    kind = ProviderFailureKind.INVALID_MODEL


class ProviderRateLimitError(ProviderTransientError):
    kind = ProviderFailureKind.RATE_LIMIT


class ProviderServerError(ProviderTransientError):
    kind = ProviderFailureKind.TRANSIENT_SERVER


class ProviderTimeoutError(ProviderTransientError):
    kind = ProviderFailureKind.TIMEOUT


def indicates_insufficient_quota(value: object) -> bool:
    """Conservative cross-provider signal for permanent credit exhaustion."""

    message = str(value).lower()
    return any(
        marker in message
        for marker in (
            "insufficient_quota",
            "credit balance",
            "billing quota",
            "quota exhausted",
            "resource_exhausted: quota",
        )
    )


def retry_after_seconds_from_http_response(value: object) -> float | None:
    """Extract a ``Retry-After`` header (seconds) from an SDK exception
    that carries an ``httpx.Response`` on ``.response`` -- Anthropic's
    and OpenAI's ``RateLimitError`` both do. Defensive by construction
    (only ``getattr``, never an attribute-error): a provider SDK that
    doesn't expose ``.response``/``.headers`` this way, or a response
    without the header, simply yields ``None`` -- the caller then falls
    back to exponential backoff, never a crash."""

    headers = getattr(getattr(value, "response", None), "headers", None)
    if headers is None:
        return None
    raw = headers.get("retry-after")
    if raw is None:
        return None
    try:
        seconds = float(raw)
    except (TypeError, ValueError):
        return None
    return seconds if seconds >= 0 else None


@dataclass(frozen=True, slots=True)
class ProviderIdentity:
    """The exact provider/model identity a request was served by -- folded
    into review-run identity so a model or provider swap never silently
    reuses a prior run's persisted results (see
    :mod:`patchfrog.review.config`)."""

    provider: str
    model: str
    extra: dict[str, str] = field(default_factory=dict)


class LLMProvider(Protocol):
    """Provider-neutral structured-output interface.

    Implementations: :class:`patchfrog.review.providers.anthropic.AnthropicLLMProvider`
    (real, Claude API) and :class:`patchfrog.review.providers.fake.FakeLLMProvider`
    (deterministic, test-only). Both raise :class:`ProviderTransientError`
    for safe-to-retry failures and :class:`ProviderFatalError` for
    everything else -- callers (see
    :mod:`patchfrog.review.service`) branch on that distinction, never on
    provider-specific exception types.
    """

    @property
    def identity(self) -> ProviderIdentity: ...

    async def generate_structured(self, request: ProviderRequest) -> ProviderResult: ...
