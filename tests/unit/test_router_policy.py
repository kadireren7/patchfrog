"""Z14 -- Model Router + governance policy corpus: credentialed-but-
forbidden providers are never selected, for reviewer, critic, or
runtime fallback alike (spec tests Z10-Z12)."""

from __future__ import annotations

from typing import Any

import pytest

from patchfrog.config.settings import Settings
from patchfrog.review.runtime_config import ReviewRuntimeConfig
from patchfrog.routing.domain import RouteReason
from patchfrog.routing.router import ModelRouter, ProviderNotAllowedByPolicyError


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


def _runtime_config(*, provider: str = "anthropic") -> ReviewRuntimeConfig:
    return ReviewRuntimeConfig(provider=provider, model="claude-opus-5", critic_model="claude-opus-5", request_timeout_seconds=30.0)


def test_no_allowed_providers_restriction_is_byte_identical_to_pre_governance_behavior() -> None:
    unrestricted = ModelRouter(settings=_settings(ANTHROPIC_API_KEY="x", GEMINI_API_KEY="y"))
    plan = unrestricted.route(runtime_config=_runtime_config(), critic_enabled=True)
    assert plan.reviewer_provider_family == "anthropic"
    assert plan.critic_provider_family == "gemini"


def test_credentialed_but_forbidden_provider_is_never_used_as_critic() -> None:
    settings = _settings(ANTHROPIC_API_KEY="x", GEMINI_API_KEY="y")
    router = ModelRouter(settings=settings, allowed_providers=frozenset({"anthropic"}))
    plan = router.route(runtime_config=_runtime_config(provider="anthropic"), critic_enabled=True)
    assert plan.reviewer_provider_family == "anthropic"
    # Only one allowed provider -- critic falls back to same family, never gemini.
    assert plan.critic_provider_family == "anthropic"
    assert RouteReason.PROVIDER_EXCLUDED_BY_POLICY in plan.reasons


def test_credentialed_but_forbidden_provider_is_never_used_as_reviewer() -> None:
    # Preferred (anthropic) is forbidden by policy -- exactly like having
    # no credential for it at all, the operator must have an explicit
    # fallback configured; policy never silently redirects on its own.
    settings = _settings(ANTHROPIC_API_KEY="x", GEMINI_API_KEY="y", PATCHFROG_ROUTER_FALLBACK_PROVIDER="gemini")
    router = ModelRouter(settings=settings, allowed_providers=frozenset({"gemini"}))
    plan = router.route(runtime_config=_runtime_config(provider="anthropic"), critic_enabled=True)
    assert plan.reviewer_provider_family == "gemini"


def test_forbidden_preferred_provider_with_no_operator_fallback_raises() -> None:
    settings = _settings(ANTHROPIC_API_KEY="x", GEMINI_API_KEY="y")
    router = ModelRouter(settings=settings, allowed_providers=frozenset({"gemini"}))
    with pytest.raises(Exception):  # noqa: B017 -- MissingProviderCredentialsError, exact same shape as "no credential"
        router.route(runtime_config=_runtime_config(provider="anthropic"), critic_enabled=True)


def test_runtime_fallback_also_respects_the_allowlist() -> None:
    settings = _settings(
        ANTHROPIC_API_KEY="x", GEMINI_API_KEY="y", OPENAI_API_KEY="z",
        PATCHFROG_ROUTER_FALLBACK_PROVIDER="gemini",
    )
    router = ModelRouter(settings=settings, allowed_providers=frozenset({"anthropic", "openai"}))
    plan = router.route(runtime_config=_runtime_config(provider="anthropic"), critic_enabled=False)
    # gemini is credentialed and explicitly configured as the runtime
    # fallback, but policy forbids it -- no runtime fallback is offered.
    assert plan.reviewer_fallback_providers is None


def test_every_credentialed_provider_forbidden_raises_provider_not_allowed() -> None:
    settings = _settings(ANTHROPIC_API_KEY="x", GEMINI_API_KEY="y")
    router = ModelRouter(settings=settings, allowed_providers=frozenset({"openai"}))
    with pytest.raises(ProviderNotAllowedByPolicyError):
        router.route(runtime_config=_runtime_config(provider="anthropic"), critic_enabled=True)


def test_provider_not_allowed_is_a_missing_credentials_error_subclass_for_existing_call_sites() -> None:
    from patchfrog.review.provider_factory import MissingProviderCredentialsError

    assert issubclass(ProviderNotAllowedByPolicyError, MissingProviderCredentialsError)
