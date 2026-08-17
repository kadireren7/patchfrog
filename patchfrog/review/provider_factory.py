"""Constructs the real (non-fake) :class:`~patchfrog.review.provider.LLMProvider`
for a given :class:`~patchfrog.review.config.ReviewConfig`.

The only place credentials are read from the environment for the AI
Reviewer -- never from ``.patchfrog.yml`` (see
:func:`patchfrog.review.config.load_review_config`), never logged.
"""

from __future__ import annotations

from patchfrog.config.settings import Settings
from patchfrog.review.config import ReviewConfig
from patchfrog.review.provider import LLMProvider
from patchfrog.review.providers.anthropic_provider import AnthropicLLMProvider


class MissingProviderCredentialsError(RuntimeError):
    """Raised when a real provider is requested but no credential is
    configured. Always a clear, actionable error -- never a crash deep in
    an SDK client constructor."""


def build_reviewer_provider(config: ReviewConfig, *, settings: Settings) -> LLMProvider:
    return _build(config.provider, config.model, settings=settings, timeout_seconds=config.request_timeout_seconds)


def build_critic_provider(config: ReviewConfig, *, settings: Settings) -> LLMProvider | None:
    if not config.critic_enabled:
        return None
    return _build(
        config.provider, config.critic_model, settings=settings, timeout_seconds=config.request_timeout_seconds
    )


def _build(provider: str, model: str, *, settings: Settings, timeout_seconds: float) -> LLMProvider:
    if provider != "anthropic":
        raise ValueError(f"unsupported review provider: {provider!r} (only 'anthropic' is implemented)")
    if not settings.anthropic_api_key:
        raise MissingProviderCredentialsError(
            "ANTHROPIC_API_KEY is not set. Set it in the environment or a secret store "
            "(never in .patchfrog.yml) before running a real AI review. "
            "Use --dry-run to build candidates/context without calling the provider."
        )
    return AnthropicLLMProvider(
        api_key=settings.anthropic_api_key, model=model, timeout_seconds=timeout_seconds
    )
