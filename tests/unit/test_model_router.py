"""Model Router (U3/U4) corpus. Every test constructs a real
:class:`~patchfrog.config.settings.Settings` (fake, non-real credentials)
and asserts on the real, deterministic :class:`ReviewRoutePlan` --
never a mocked router. Real provider adapter instances (never
FakeLLMProvider) are constructed by the router itself, exactly as
production would, but no network call is ever made (constructing an
adapter never calls out)."""

from __future__ import annotations

import inspect
from typing import Any

import pytest

from patchfrog.config.settings import Settings
from patchfrog.review.agents.roles import AgentRole
from patchfrog.review.provider_factory import MissingProviderCredentialsError
from patchfrog.review.providers.anthropic_provider import AnthropicLLMProvider
from patchfrog.review.providers.gemini_provider import GeminiLLMProvider
from patchfrog.review.providers.openai_provider import OpenAILLMProvider
from patchfrog.review.runtime_config import ReviewRuntimeConfig
from patchfrog.routing.domain import RouteReason
from patchfrog.routing.router import ModelRouter, NoProviderConfiguredError

_ADAPTER_CLASS = {
    "anthropic": AnthropicLLMProvider,
    "gemini": GeminiLLMProvider,
    "openai": OpenAILLMProvider,
}


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
    *, provider: str = "anthropic", model: str = "claude-opus-5", critic_model: str | None = None,
) -> ReviewRuntimeConfig:
    return ReviewRuntimeConfig(
        provider=provider, model=model, critic_model=critic_model or model, request_timeout_seconds=30.0,
    )


# -- Single-provider configurations (items 1-3 of the required matrix) --


@pytest.mark.parametrize("provider", ["anthropic", "gemini", "openai"])
def test_only_one_provider_configured_routes_reviewer_and_critic_to_it(provider: str) -> None:
    key_field = {"anthropic": "ANTHROPIC_API_KEY", "gemini": "GEMINI_API_KEY", "openai": "OPENAI_API_KEY"}[provider]
    router = ModelRouter(settings=_settings(**{key_field: "fake-not-real"}))
    plan = router.route(runtime_config=_runtime_config(provider=provider), critic_enabled=True)

    assert plan.reviewer_provider_family == provider
    assert plan.critic_provider_family == provider
    assert plan.diversity_available is False
    assert plan.diversity_used is False
    assert plan.config_fallback_used is False
    assert RouteReason.SINGLE_PROVIDER_CONFIGURED in plan.reasons
    assert isinstance(plan.reviewer_providers[AgentRole.CORRECTNESS], _ADAPTER_CLASS[provider])
    assert isinstance(plan.reviewer_providers[AgentRole.SECURITY], _ADAPTER_CLASS[provider])
    assert isinstance(plan.critic_provider, _ADAPTER_CLASS[provider])


# -- Multi-provider combinations (items 4-7 of the required matrix) --


def test_two_providers_configured_uses_auto_diversity_for_critic() -> None:
    router = ModelRouter(
        settings=_settings(ANTHROPIC_API_KEY="fake-not-real", GEMINI_API_KEY="fake-not-real")
    )
    plan = router.route(runtime_config=_runtime_config(provider="anthropic"), critic_enabled=True)

    assert plan.reviewer_provider_family == "anthropic"
    assert plan.critic_provider_family == "gemini"
    assert plan.diversity_available is True
    assert plan.diversity_used is True
    assert RouteReason.CRITIC_FAMILY_DIVERSITY_USED in plan.reasons


def test_gemini_and_openai_configured_diversity_works_for_any_pair() -> None:
    router = ModelRouter(settings=_settings(GEMINI_API_KEY="fake-not-real", OPENAI_API_KEY="fake-not-real"))
    plan = router.route(runtime_config=_runtime_config(provider="gemini"), critic_enabled=True)

    assert plan.reviewer_provider_family == "gemini"
    assert plan.critic_provider_family == "openai"
    assert plan.diversity_used is True


def test_anthropic_and_openai_configured_diversity_works() -> None:
    router = ModelRouter(settings=_settings(ANTHROPIC_API_KEY="fake-not-real", OPENAI_API_KEY="fake-not-real"))
    plan = router.route(runtime_config=_runtime_config(provider="anthropic"), critic_enabled=True)

    assert plan.reviewer_provider_family == "anthropic"
    assert plan.critic_provider_family == "openai"
    assert plan.diversity_used is True


def test_all_three_providers_configured_prefers_configured_reviewer_and_diverse_critic() -> None:
    router = ModelRouter(
        settings=_settings(
            ANTHROPIC_API_KEY="fake-not-real", GEMINI_API_KEY="fake-not-real", OPENAI_API_KEY="fake-not-real",
        )
    )
    plan = router.route(runtime_config=_runtime_config(provider="openai"), critic_enabled=True)

    assert plan.reviewer_provider_family == "openai"
    assert plan.critic_provider_family in ("anthropic", "gemini")
    assert plan.diversity_available is True
    assert plan.diversity_used is True


# -- No provider configured (item 8) --


def test_no_provider_configured_raises_clear_error() -> None:
    with pytest.raises(NoProviderConfiguredError):
        ModelRouter(settings=_settings()).route(runtime_config=_runtime_config(), critic_enabled=True)


def test_no_provider_configured_error_is_a_missing_credentials_error() -> None:
    # Reuses the existing exception domain (Part N) -- any call site
    # that already handles MissingProviderCredentialsError handles this too.
    assert issubclass(NoProviderConfiguredError, MissingProviderCredentialsError)


# -- Preferred provider timeout/unavailable + fallback semantics (items 9-13) --


def test_preferred_unavailable_but_fallback_configured_and_credentialed_is_used() -> None:
    router = ModelRouter(
        settings=_settings(
            GEMINI_API_KEY="fake-not-real", PATCHFROG_ROUTER_FALLBACK_PROVIDER="gemini",
        )
    )
    plan = router.route(runtime_config=_runtime_config(provider="anthropic"), critic_enabled=True)

    assert plan.reviewer_provider_family == "gemini"
    assert plan.config_fallback_used is True
    assert RouteReason.PREFERRED_PROVIDER_UNAVAILABLE_FALLBACK_USED in plan.reasons


def test_preferred_unavailable_and_fallback_also_unavailable_raises() -> None:
    router = ModelRouter(
        settings=_settings(PATCHFROG_ROUTER_FALLBACK_PROVIDER="gemini")  # no GEMINI_API_KEY set
    )
    with pytest.raises(MissingProviderCredentialsError):
        router.route(runtime_config=_runtime_config(provider="anthropic"), critic_enabled=True)


def test_preferred_unavailable_no_fallback_configured_at_all_raises() -> None:
    router = ModelRouter(settings=_settings(GEMINI_API_KEY="fake-not-real"))  # anthropic preferred, not configured
    with pytest.raises(MissingProviderCredentialsError):
        router.route(runtime_config=_runtime_config(provider="anthropic"), critic_enabled=True)


def test_fallback_never_chains_to_a_second_fallback() -> None:
    # Settings only ever has one router_fallback_provider field -- there
    # is structurally no way to configure a fallback-of-a-fallback. This
    # documents that bound explicitly rather than leaving it implicit.
    fallback_fields = [name for name in Settings.model_fields if "fallback" in name.lower()]
    assert fallback_fields == ["router_fallback_provider"]


# -- Explicit critic-family override (item 17-18 of required matrix) --


def test_explicit_critic_provider_override_is_honored_when_configured() -> None:
    router = ModelRouter(
        settings=_settings(
            ANTHROPIC_API_KEY="fake-not-real", GEMINI_API_KEY="fake-not-real", OPENAI_API_KEY="fake-not-real",
            PATCHFROG_ROUTER_CRITIC_PROVIDER="openai",
        )
    )
    plan = router.route(runtime_config=_runtime_config(provider="anthropic"), critic_enabled=True)

    assert plan.critic_provider_family == "openai"
    assert plan.diversity_used is True


def test_explicit_critic_provider_pinned_to_same_family_as_reviewer_is_honored() -> None:
    router = ModelRouter(
        settings=_settings(
            ANTHROPIC_API_KEY="fake-not-real", GEMINI_API_KEY="fake-not-real",
            PATCHFROG_ROUTER_CRITIC_PROVIDER="anthropic",
        )
    )
    plan = router.route(runtime_config=_runtime_config(provider="anthropic"), critic_enabled=True)

    assert plan.critic_provider_family == "anthropic"
    assert plan.diversity_used is False
    assert RouteReason.CRITIC_SAME_FAMILY_NO_ALTERNATIVE in plan.reasons


def test_explicit_critic_provider_without_its_own_credential_falls_back_to_auto_diversity() -> None:
    # PATCHFROG_ROUTER_CRITIC_PROVIDER names a provider that isn't
    # actually credentialed -- never silently used; auto-diversity logic
    # still applies among what IS actually configured.
    router = ModelRouter(
        settings=_settings(
            ANTHROPIC_API_KEY="fake-not-real", GEMINI_API_KEY="fake-not-real",
            PATCHFROG_ROUTER_CRITIC_PROVIDER="openai",  # no OPENAI_API_KEY
        )
    )
    plan = router.route(runtime_config=_runtime_config(provider="anthropic"), critic_enabled=True)

    assert plan.critic_provider_family == "gemini"  # auto-diversity, not the uncredentialed openai


# -- Critic disabled (item related to Part K "critic if required") --


def test_critic_disabled_produces_no_critic_route() -> None:
    router = ModelRouter(
        settings=_settings(ANTHROPIC_API_KEY="fake-not-real", GEMINI_API_KEY="fake-not-real")
    )
    plan = router.route(runtime_config=_runtime_config(provider="anthropic"), critic_enabled=False)

    assert plan.critic_provider is None
    assert plan.critic_provider_family is None
    assert RouteReason.CRITIC_DISABLED_BY_CONFIG in plan.reasons


# -- Determinism, secret hygiene, and structural boundaries (items 19-24) --


def test_route_is_deterministic_for_identical_settings() -> None:
    settings = _settings(ANTHROPIC_API_KEY="fake-not-real", GEMINI_API_KEY="fake-not-real")
    router = ModelRouter(settings=settings)
    plan_a = router.route(runtime_config=_runtime_config(provider="anthropic"), critic_enabled=True)
    plan_b = router.route(runtime_config=_runtime_config(provider="anthropic"), critic_enabled=True)

    assert plan_a.reviewer_provider_family == plan_b.reviewer_provider_family
    assert plan_a.critic_provider_family == plan_b.critic_provider_family
    assert plan_a.reasons == plan_b.reasons


def test_provider_secret_never_appears_in_route_plan_repr() -> None:
    secret = "sk-super-secret-value-not-real"
    router = ModelRouter(settings=_settings(ANTHROPIC_API_KEY=secret))
    plan = router.route(runtime_config=_runtime_config(provider="anthropic"), critic_enabled=True)

    assert secret not in repr(plan)
    assert secret not in str(plan.reviewer_provider_family)


def test_route_signature_takes_no_repository_content_parameter() -> None:
    # Structural boundary: the router's only inputs are operator config
    # (via Settings, injected at construction) and an already-resolved
    # ReviewRuntimeConfig -- nothing shaped like diff/PR/comment content
    # can reach routing decisions, by construction of this signature.
    params = set(inspect.signature(ModelRouter.route).parameters)
    assert params == {"self", "runtime_config", "critic_enabled"}


def test_reviewer_providers_mapping_covers_both_existing_roles() -> None:
    router = ModelRouter(settings=_settings(ANTHROPIC_API_KEY="fake-not-real"))
    plan = router.route(runtime_config=_runtime_config(provider="anthropic"), critic_enabled=True)

    assert set(plan.reviewer_providers) == {AgentRole.CORRECTNESS, AgentRole.SECURITY}
    assert plan.reviewer_providers[AgentRole.CORRECTNESS] is plan.reviewer_providers[AgentRole.SECURITY]


def test_version_is_one() -> None:
    router = ModelRouter(settings=_settings(ANTHROPIC_API_KEY="fake-not-real"))
    plan = router.route(runtime_config=_runtime_config(provider="anthropic"), critic_enabled=True)
    assert plan.version == 1


# -- Runtime fallback must be explicit (final correction) --


def test_multiple_providers_configured_but_no_fallback_setting_means_no_runtime_fallback() -> None:
    # A credential existing for gemini is NOT, by itself, permission to
    # use it as a runtime fallback -- PATCHFROG_ROUTER_FALLBACK_PROVIDER
    # must be explicitly set, or there is no runtime fallback at all,
    # even though two providers are configured and critic diversity
    # (a separate, config-time concept) legitimately uses gemini.
    router = ModelRouter(
        settings=_settings(ANTHROPIC_API_KEY="fake-not-real", GEMINI_API_KEY="fake-not-real")
    )
    plan = router.route(runtime_config=_runtime_config(provider="anthropic"), critic_enabled=True)

    assert plan.reviewer_fallback_providers is None
    assert plan.reviewer_runtime_fallback_family is None
    assert plan.critic_fallback_provider is None
    assert plan.critic_runtime_fallback_family is None
    assert plan.runtime_fallback_permitted is False
    # Critic diversity itself is untouched by this correction.
    assert plan.critic_provider_family == "gemini"


def test_explicit_fallback_provider_setting_permits_runtime_fallback() -> None:
    router = ModelRouter(
        settings=_settings(
            ANTHROPIC_API_KEY="fake-not-real", GEMINI_API_KEY="fake-not-real",
            PATCHFROG_ROUTER_FALLBACK_PROVIDER="gemini",
        )
    )
    plan = router.route(runtime_config=_runtime_config(provider="anthropic"), critic_enabled=True)

    assert plan.reviewer_fallback_providers is not None
    assert plan.reviewer_runtime_fallback_family == "gemini"
    assert plan.runtime_fallback_permitted is True
    assert isinstance(plan.reviewer_fallback_providers[AgentRole.CORRECTNESS], GeminiLLMProvider)


def test_explicit_fallback_naming_the_primary_itself_permits_no_fallback() -> None:
    # The fallback setting names the same family that is already primary
    # -- there is no *distinct* provider to fail over to, so this must
    # still resolve to "no runtime fallback", not a fallback-to-self.
    router = ModelRouter(
        settings=_settings(
            ANTHROPIC_API_KEY="fake-not-real", GEMINI_API_KEY="fake-not-real",
            PATCHFROG_ROUTER_FALLBACK_PROVIDER="anthropic",
        )
    )
    plan = router.route(runtime_config=_runtime_config(provider="anthropic"), critic_enabled=True)

    assert plan.reviewer_fallback_providers is None
    assert plan.reviewer_runtime_fallback_family is None


def test_fallback_setting_naming_an_uncredentialed_provider_permits_no_runtime_fallback() -> None:
    # PATCHFROG_ROUTER_FALLBACK_PROVIDER names openai, but OPENAI_API_KEY
    # is not set -- having a *name* configured is not the same as having
    # a usable, credentialed provider to fail over to.
    router = ModelRouter(
        settings=_settings(
            ANTHROPIC_API_KEY="fake-not-real", GEMINI_API_KEY="fake-not-real",
            PATCHFROG_ROUTER_FALLBACK_PROVIDER="openai",
        )
    )
    plan = router.route(runtime_config=_runtime_config(provider="anthropic"), critic_enabled=True)

    assert plan.reviewer_fallback_providers is None
    assert plan.reviewer_runtime_fallback_family is None
