"""patchfrog.review.provider_factory routing: anthropic -> AnthropicLLMProvider,
gemini -> GeminiLLMProvider, unknown provider -> a clear ValueError, missing
credential -> MissingProviderCredentialsError for whichever provider was
actually requested. Never silently falls back to another provider or to
FakeLLMProvider."""

from __future__ import annotations

from typing import Any

import pytest

from patchfrog.config.settings import Settings
from patchfrog.review.config import ReviewConfig
from patchfrog.review.provider_factory import (
    MissingProviderCredentialsError,
    build_critic_provider,
    build_reviewer_provider,
)
from patchfrog.review.providers.anthropic_provider import AnthropicLLMProvider
from patchfrog.review.providers.gemini_provider import GeminiLLMProvider


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


def test_anthropic_provider_routes_to_anthropic_client() -> None:
    provider = build_reviewer_provider(
        ReviewConfig(provider="anthropic", model="claude-opus-5"),
        settings=_settings(ANTHROPIC_API_KEY="fake-not-real"),
    )
    assert isinstance(provider, AnthropicLLMProvider)
    assert provider.identity.provider == "anthropic"


def test_gemini_provider_routes_to_gemini_client() -> None:
    provider = build_reviewer_provider(
        ReviewConfig(provider="gemini", model="gemini-3.6-flash"),
        settings=_settings(GEMINI_API_KEY="fake-not-real"),
    )
    assert isinstance(provider, GeminiLLMProvider)
    assert provider.identity.provider == "gemini"


def test_gemini_critic_provider_also_routes_correctly() -> None:
    provider = build_critic_provider(
        ReviewConfig(provider="gemini", model="gemini-3.6-flash", critic_model="gemini-3.6-flash"),
        settings=_settings(GEMINI_API_KEY="fake-not-real"),
    )
    assert isinstance(provider, GeminiLLMProvider)


def test_missing_gemini_credential_raises_clear_error() -> None:
    with pytest.raises(MissingProviderCredentialsError, match="GEMINI_API_KEY"):
        build_reviewer_provider(
            ReviewConfig(provider="gemini", model="gemini-3.6-flash"),
            settings=_settings(),
        )


def test_missing_anthropic_credential_raises_clear_error() -> None:
    with pytest.raises(MissingProviderCredentialsError, match="ANTHROPIC_API_KEY"):
        build_reviewer_provider(
            ReviewConfig(provider="anthropic", model="claude-opus-5"),
            settings=_settings(),
        )


def test_missing_gemini_credential_never_falls_back_to_anthropic() -> None:
    # Even with a real-looking Anthropic key present, requesting gemini
    # with no Gemini key must fail loudly, never silently substitute
    # another provider.
    with pytest.raises(MissingProviderCredentialsError, match="GEMINI_API_KEY"):
        build_reviewer_provider(
            ReviewConfig(provider="gemini", model="gemini-3.6-flash"),
            settings=_settings(ANTHROPIC_API_KEY="fake-not-real"),
        )


def test_unknown_provider_raises_clear_value_error() -> None:
    with pytest.raises(ValueError, match="unsupported review provider"):
        build_reviewer_provider(
            ReviewConfig(provider="openai", model="gpt-x"),
            settings=_settings(),
        )
