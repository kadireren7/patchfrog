"""patchfrog.review.provider_factory routing: anthropic -> AnthropicLLMProvider,
gemini -> GeminiLLMProvider, unknown provider -> a clear ValueError, missing
credential -> MissingProviderCredentialsError for whichever provider was
actually requested. Never silently falls back to another provider or to
FakeLLMProvider. All provider selection comes from ReviewRuntimeConfig
(operator-controlled), never from ReviewConfig (repository-controlled)."""

from __future__ import annotations

from typing import Any

import pytest

from patchfrog.config.settings import Settings
from patchfrog.review.provider_factory import (
    MissingProviderCredentialsError,
    build_critic_provider,
    build_reviewer_provider,
)
from patchfrog.review.providers.anthropic_provider import AnthropicLLMProvider
from patchfrog.review.providers.gemini_provider import GeminiLLMProvider
from patchfrog.review.runtime_config import ReviewRuntimeConfig


def _settings(**overrides: object) -> Settings:
    base: dict[str, Any] = {
        "DATABASE_URL": "sqlite+aiosqlite:///:memory:",
        "REDIS_URL": "redis://localhost:6379/0",
        "GITHUB_APP_ID": "1",
        "GITHUB_PRIVATE_KEY": "-----BEGIN PRIVATE KEY-----\nx\n-----END PRIVATE KEY-----",
        "GITHUB_WEBHOOK_SECRET": "x",
    }
    base.update(overrides)
    return Settings(**base)


def _runtime_config(
    *, provider: str = "anthropic", model: str = "claude-opus-5",
    critic_model: str | None = None, request_timeout_seconds: float = 30.0,
) -> ReviewRuntimeConfig:
    return ReviewRuntimeConfig(
        provider=provider,
        model=model,
        critic_model=critic_model if critic_model is not None else model,
        request_timeout_seconds=request_timeout_seconds,
    )


def test_anthropic_provider_routes_to_anthropic_client() -> None:
    provider = build_reviewer_provider(
        _runtime_config(provider="anthropic", model="claude-opus-5"),
        settings=_settings(ANTHROPIC_API_KEY="fake-not-real"),
    )
    assert isinstance(provider, AnthropicLLMProvider)
    assert provider.identity.provider == "anthropic"


def test_gemini_provider_routes_to_gemini_client() -> None:
    provider = build_reviewer_provider(
        _runtime_config(provider="gemini", model="gemini-3.6-flash"),
        settings=_settings(GEMINI_API_KEY="fake-not-real"),
    )
    assert isinstance(provider, GeminiLLMProvider)
    assert provider.identity.provider == "gemini"


def test_gemini_critic_provider_also_routes_correctly() -> None:
    provider = build_critic_provider(
        _runtime_config(provider="gemini", model="gemini-3.6-flash", critic_model="gemini-3.6-flash"),
        settings=_settings(GEMINI_API_KEY="fake-not-real"),
        critic_enabled=True,
    )
    assert isinstance(provider, GeminiLLMProvider)


def test_critic_disabled_returns_none_regardless_of_provider() -> None:
    provider = build_critic_provider(
        _runtime_config(provider="anthropic", model="claude-opus-5"),
        settings=_settings(ANTHROPIC_API_KEY="fake-not-real"),
        critic_enabled=False,
    )
    assert provider is None


def test_missing_gemini_credential_raises_clear_error() -> None:
    with pytest.raises(MissingProviderCredentialsError, match="GEMINI_API_KEY"):
        build_reviewer_provider(
            _runtime_config(provider="gemini", model="gemini-3.6-flash"),
            settings=_settings(),
        )


def test_missing_anthropic_credential_raises_clear_error() -> None:
    with pytest.raises(MissingProviderCredentialsError, match="ANTHROPIC_API_KEY"):
        build_reviewer_provider(
            _runtime_config(provider="anthropic", model="claude-opus-5"),
            settings=_settings(),
        )


def test_missing_gemini_credential_never_falls_back_to_anthropic() -> None:
    # Even with a real-looking Anthropic key present, requesting gemini
    # with no Gemini key must fail loudly, never silently substitute
    # another provider.
    with pytest.raises(MissingProviderCredentialsError, match="GEMINI_API_KEY"):
        build_reviewer_provider(
            _runtime_config(provider="gemini", model="gemini-3.6-flash"),
            settings=_settings(ANTHROPIC_API_KEY="fake-not-real"),
        )


def test_unknown_provider_raises_clear_value_error() -> None:
    with pytest.raises(ValueError, match="unsupported review provider"):
        build_reviewer_provider(
            _runtime_config(provider="openai", model="gpt-x"),
            settings=_settings(),
        )


def test_minimal_gemini_config_builds_reviewer_and_critic_without_claude_opus_5() -> None:
    """G: the minimal documented Gemini runtime config (provider + model,
    critic_model defaulted by resolve_review_runtime_config) must build a
    Gemini critic using the Gemini model -- never request claude-opus-5
    from Gemini's API. Regression for a real bug found live: an omitted
    critic_model previously stayed the Anthropic default regardless of
    provider."""

    runtime_config = _runtime_config(provider="gemini", model="gemini-3.6-flash")
    settings = _settings(GEMINI_API_KEY="fake-not-real")

    reviewer = build_reviewer_provider(runtime_config, settings=settings)
    critic = build_critic_provider(runtime_config, settings=settings, critic_enabled=True)

    assert isinstance(reviewer, GeminiLLMProvider)
    assert reviewer.identity.model == "gemini-3.6-flash"
    assert isinstance(critic, GeminiLLMProvider)
    assert critic is not None
    assert critic.identity.model == "gemini-3.6-flash"
    assert critic.identity.model != "claude-opus-5"
