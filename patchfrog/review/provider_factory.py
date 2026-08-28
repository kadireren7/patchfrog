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
        return AnthropicLLMProvider(
            api_key=settings.anthropic_api_key, model=model, timeout_seconds=timeout_seconds
        )
    if provider == "gemini":
        if not settings.gemini_api_key:
            raise MissingProviderCredentialsError(
                "GEMINI_API_KEY is not set. Set it in the environment or a secret store "
                "(never in .patchfrog.yml) before running a real AI review. "
                "Use --dry-run to build candidates/context without calling the provider."
            )
        return GeminiLLMProvider(
            api_key=settings.gemini_api_key, model=model, timeout_seconds=timeout_seconds
        )
    raise ValueError(
        f"unsupported review provider: {provider!r} (supported: {', '.join(SUPPORTED_PROVIDERS)})"
    )
