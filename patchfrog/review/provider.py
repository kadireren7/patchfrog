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
    """Base class for every provider failure."""


class ProviderTransientError(ProviderError):
    """A failure that is safe to retry with bounded backoff: rate limits,
    server-side overload, or a dropped connection. Never raised for
    anything that would repeat identically on retry."""


class ProviderFatalError(ProviderError):
    """A failure that must never be retried: an auth failure, a malformed
    request (HTTP 400), or a response that doesn't parse against the
    requested schema. Retrying would just repeat the same failure."""


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
