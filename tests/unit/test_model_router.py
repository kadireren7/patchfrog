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
from patchfrog.diff.parser import build_diff_file
from patchfrog.review.agents.roles import AgentRole
from patchfrog.review.provider_factory import MissingProviderCredentialsError
from patchfrog.review.providers.anthropic_provider import AnthropicLLMProvider
from patchfrog.review.providers.gemini_provider import GeminiLLMProvider
from patchfrog.review.providers.openai_provider import OpenAILLMProvider
from patchfrog.review.runtime_config import DEFAULT_MODEL_BY_PROVIDER, ReviewRuntimeConfig
from patchfrog.routing.domain import RouteReason
from patchfrog.routing.router import ModelRouter, NoProviderConfiguredError, is_small_review

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
    *, provider: str = "anthropic", model: str | None = None, critic_model: str | None = None,
) -> ReviewRuntimeConfig:
    # Mirrors resolve_review_runtime_config's own provider-aware default
    # (model omitted -> that provider's own model, never a flat
    # anthropic-shaped default) -- a test helper that hardcoded
    # "claude-opus-5" regardless of `provider` would itself have masked
    # the exact production bug this module's own router fix addresses.
    resolved_model = model if model is not None else DEFAULT_MODEL_BY_PROVIDER[provider]
    return ReviewRuntimeConfig(
        provider=provider, model=resolved_model, critic_model=critic_model or resolved_model,
        request_timeout_seconds=30.0,
    )


def test_small_review_uses_explicit_operator_cheap_route() -> None:
    router = ModelRouter(
        settings=_settings(
            ANTHROPIC_API_KEY="fake-not-real",
            GEMINI_API_KEY="fake-not-real",
            PATCHFROG_ROUTER_CHEAP_PROVIDER="gemini",
            PATCHFROG_ROUTER_CHEAP_MODEL="gemini-cheap-test",
        )
    )

    plan = router.route(
        runtime_config=_runtime_config(provider="anthropic"),
        critic_enabled=True,
        prefer_low_cost=True,
    )

    assert plan.reviewer_provider_family == "gemini"
    assert plan.reviewer_providers[AgentRole.CORRECTNESS].identity.model == "gemini-cheap-test"
    assert RouteReason.CHEAP_ROUTE_USED in plan.reasons


def test_cheap_route_cannot_bypass_allowed_provider_policy() -> None:
    router = ModelRouter(
        settings=_settings(
            ANTHROPIC_API_KEY="fake-not-real",
            GEMINI_API_KEY="fake-not-real",
            PATCHFROG_ROUTER_CHEAP_PROVIDER="gemini",
        ),
        allowed_providers=frozenset({"anthropic"}),
    )

    plan = router.route(
        runtime_config=_runtime_config(provider="anthropic"),
        critic_enabled=False,
        prefer_low_cost=True,
    )

    assert plan.reviewer_provider_family == "anthropic"
    assert RouteReason.CHEAP_ROUTE_USED not in plan.reasons


def test_small_review_signal_is_bounded_by_files_and_changed_lines() -> None:
    tiny = build_diff_file("a.py", "@@ -1 +1 @@\n-old\n+new\n")
    assert is_small_review([tiny]) is True

    large_patch = "@@ -1,81 +1,81 @@\n" + "".join(f"-old{i}\n+new{i}\n" for i in range(41))
    assert is_small_review([build_diff_file("a.py", large_patch)]) is False


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
    # The actual production bug: provider selection alone isn't enough --
    # the constructed provider's own model must belong to that provider's
    # family, never a different family's model name reaching its API.
    assert plan.reviewer_providers[AgentRole.CORRECTNESS].identity.model == DEFAULT_MODEL_BY_PROVIDER[provider]
    assert plan.critic_provider is not None
    assert plan.critic_provider.identity.model == DEFAULT_MODEL_BY_PROVIDER[provider]


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
    # The actual production bug: falling back to gemini must also fall
    # back to a gemini-shaped model, never the anthropic model the
    # reviewer was originally (but no longer) configured for.
    assert plan.reviewer_providers[AgentRole.CORRECTNESS].identity.model == DEFAULT_MODEL_BY_PROVIDER["gemini"]


def test_preferred_anthropic_unavailable_fallback_openai_gets_an_openai_model() -> None:
    # Requirement 7, scenario 1 -- the exact reported production shape:
    # ANTHROPIC_API_KEY missing, OPENAI_API_KEY present, provider falls
    # back to openai and must get an openai-shaped model.
    router = ModelRouter(
        settings=_settings(OPENAI_API_KEY="fake-not-real", PATCHFROG_ROUTER_FALLBACK_PROVIDER="openai")
    )
    plan = router.route(runtime_config=_runtime_config(provider="anthropic"), critic_enabled=False)

    assert plan.reviewer_provider_family == "openai"
    model = plan.reviewer_providers[AgentRole.CORRECTNESS].identity.model
    assert model == DEFAULT_MODEL_BY_PROVIDER["openai"]
    assert not model.startswith("claude-")


def test_preferred_anthropic_unavailable_fallback_gemini_gets_a_gemini_model() -> None:
    # Requirement 7, scenario 2.
    router = ModelRouter(
        settings=_settings(GEMINI_API_KEY="fake-not-real", PATCHFROG_ROUTER_FALLBACK_PROVIDER="gemini")
    )
    plan = router.route(runtime_config=_runtime_config(provider="anthropic"), critic_enabled=False)

    assert plan.reviewer_provider_family == "gemini"
    model = plan.reviewer_providers[AgentRole.CORRECTNESS].identity.model
    assert model == DEFAULT_MODEL_BY_PROVIDER["gemini"]
    assert not model.startswith("claude-")


def test_explicit_provider_and_compatible_model_is_preserved() -> None:
    # Requirement 7, scenario 3: an operator-configured, valid model for
    # the preferred (and available) provider must be honored exactly,
    # never silently replaced by the provider's own default.
    router = ModelRouter(settings=_settings(OPENAI_API_KEY="fake-not-real"))
    plan = router.route(
        runtime_config=_runtime_config(provider="openai", model="gpt-6-mini"), critic_enabled=False
    )

    assert plan.reviewer_providers[AgentRole.CORRECTNESS].identity.model == "gpt-6-mini"


def test_critic_on_same_fallback_family_as_reviewer_gets_that_familys_model_not_the_original_providers() -> None:
    """Router-level regression for the second half of the production
    bug: when critic_family ends up equal to a *fallen-back* reviewer
    family (not the operator's originally-configured provider), the
    critic model must also come from that family's own default -- not
    from `runtime_config.critic_model`, which was only ever valid for
    the *original* provider. Previously this compared
    `critic_family == reviewer_family` instead of `critic_family ==
    runtime_config.provider`, so a critic landing on the same fallback
    family as the reviewer still got the original provider's critic
    model."""

    router = ModelRouter(
        settings=_settings(GEMINI_API_KEY="fake-not-real", PATCHFROG_ROUTER_FALLBACK_PROVIDER="gemini")
    )
    # provider="anthropic" (unavailable) falls back to gemini for both
    # reviewer and critic (gemini is the only configured provider, so
    # SINGLE_PROVIDER_CONFIGURED routes the critic to the same family).
    # An explicit critic_model here is deliberately anthropic-shaped --
    # exactly the value an operator would have set for the *original*
    # preferred provider, before any fallback ever happened.
    plan = router.route(
        runtime_config=_runtime_config(provider="anthropic", critic_model="claude-haiku-5"),
        critic_enabled=True,
    )

    assert plan.reviewer_provider_family == "gemini"
    assert plan.critic_provider_family == "gemini"
    assert plan.critic_provider is not None
    assert plan.critic_provider.identity.model == DEFAULT_MODEL_BY_PROVIDER["gemini"]
    assert plan.critic_provider.identity.model != "claude-haiku-5"


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
    assert params == {"self", "runtime_config", "critic_enabled", "prefer_low_cost"}


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
