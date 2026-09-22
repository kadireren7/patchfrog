"""Constructs the real (non-fake) :class:`~patchfrog.review.provider.LLMProvider`
for a given :class:`~patchfrog.review.runtime_config.ReviewRuntimeConfig`.

Provider/model/timeout come exclusively from the operator-controlled
:class:`~patchfrog.review.runtime_config.ReviewRuntimeConfig` -- never
from the repository-controlled
:class:`~patchfrog.review.config.ReviewConfig`. ``critic_enabled`` is
still repository review *behavior*, so it's passed in separately rather
than folded into the runtime config (it doesn't identify a provider).

This is the only place credentials are read from the environment for the
AI Reviewer -- never from ``.patchfrog.yml`` (see
:func:`patchfrog.review.config.load_review_config`), never logged.
"""

from __future__ import annotations

from patchfrog.config.settings import Settings
from patchfrog.review.provider import LLMProvider
from patchfrog.review.providers.anthropic_provider import AnthropicLLMProvider
from patchfrog.review.providers.gemini_provider import GeminiLLMProvider
from patchfrog.review.providers.openai_provider import OpenAILLMProvider
from patchfrog.review.rate_limiter import (
    RateLimitedProvider,
    default_rate_limiter_registry,
    resolve_rate_limit_rpm,
)
from patchfrog.review.runtime_config import SUPPORTED_PROVIDERS, ReviewRuntimeConfig


class MissingProviderCredentialsError(RuntimeError):
    """Raised when a real provider is requested but no credential is
    configured. Always a clear, actionable error -- never a crash deep in
    an SDK client constructor."""


def build_reviewer_provider(runtime_config: ReviewRuntimeConfig, *, settings: Settings) -> LLMProvider:
    return _build(
        runtime_config.provider,
        runtime_config.model,
        settings=settings,
        timeout_seconds=runtime_config.request_timeout_seconds,
    )


def build_critic_provider(
    runtime_config: ReviewRuntimeConfig, *, settings: Settings, critic_enabled: bool
) -> LLMProvider | None:
    if not critic_enabled:
        return None
    return _build(
        runtime_config.provider,
        runtime_config.critic_model,
        settings=settings,
        timeout_seconds=runtime_config.request_timeout_seconds,
    )


def _build(provider: str, model: str, *, settings: Settings, timeout_seconds: float) -> LLMProvider:
    if provider == "anthropic":
        if not settings.anthropic_api_key:
            raise MissingProviderCredentialsError(
                "ANTHROPIC_API_KEY is not set. Set it in the environment or a secret store "
                "(never in .patchfrog.yml) before running a real AI review. "
                "Use --dry-run to build candidates/context without calling the provider."
            )
        client: LLMProvider = AnthropicLLMProvider(
            api_key=settings.anthropic_api_key, model=model, timeout_seconds=timeout_seconds
        )
    elif provider == "gemini":
        if not settings.gemini_api_key:
            raise MissingProviderCredentialsError(
                "GEMINI_API_KEY is not set. Set it in the environment or a secret store "
                "(never in .patchfrog.yml) before running a real AI review. "
                "Use --dry-run to build candidates/context without calling the provider."
            )
        client = GeminiLLMProvider(api_key=settings.gemini_api_key, model=model, timeout_seconds=timeout_seconds)
    elif provider == "openai":
        if not settings.openai_api_key:
            raise MissingProviderCredentialsError(
                "OPENAI_API_KEY is not set. Set it in the environment or a secret store "
                "(never in .patchfrog.yml) before running a real AI review. "
                "Use --dry-run to build candidates/context without calling the provider."
            )
        client = OpenAILLMProvider(api_key=settings.openai_api_key, model=model, timeout_seconds=timeout_seconds)
    else:
        raise ValueError(
            f"unsupported review provider: {provider!r} (supported: {', '.join(SUPPORTED_PROVIDERS)})"
        )
    rpm = resolve_rate_limit_rpm(settings.provider_rate_limit_rpm, provider=provider, model=model)
    if rpm is None:
        return client
    limiter = default_rate_limiter_registry().get(provider=provider, model=model, requests_per_minute=rpm)
    return RateLimitedProvider(client, limiter)


def has_credentials(provider: str, *, settings: Settings) -> bool:
    """Whether ``settings`` has a non-empty API key for ``provider`` --
    used by :mod:`patchfrog.routing` to determine which configured
    providers are actually usable without constructing a real client (and
    therefore without needing a model name yet). Never logs or returns
    the credential itself, only presence."""

    if provider == "anthropic":
        return bool(settings.anthropic_api_key)
    if provider == "gemini":
        return bool(settings.gemini_api_key)
    if provider == "openai":
        return bool(settings.openai_api_key)
    return False
