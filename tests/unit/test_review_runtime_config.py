"""patchfrog.review.runtime_config: operator/deployment-controlled AI
provider runtime resolution. Resolved exclusively from trusted Settings
(PATCHFROG_REVIEW_* environment variables) -- never from .patchfrog.yml
(see tests/unit/test_review_config.py for that trust-boundary coverage).

Effective-default semantics mirror the pre-Milestone-C ReviewConfig
behavior exactly (same defaults, same "omitted vs. explicit" precedence),
just relocated to the operator/runtime layer."""

from __future__ import annotations

from typing import Any

import pytest

from patchfrog.config.settings import Settings
from patchfrog.review.runtime_config import DEFAULT_MODEL_BY_PROVIDER, resolve_review_runtime_config


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


# -- B: runtime defaults (no PATCHFROG_REVIEW_* vars set at all) --


def test_no_runtime_vars_defaults_to_anthropic_claude_opus_5() -> None:
    runtime_config = resolve_review_runtime_config(_settings())
    assert runtime_config.provider == "anthropic"
    assert runtime_config.model == "claude-opus-5"


def test_no_runtime_vars_critic_model_defaults_to_reviewer_model() -> None:
    runtime_config = resolve_review_runtime_config(_settings())
    assert runtime_config.critic_model == runtime_config.model


def test_no_runtime_vars_timeout_defaults_to_30_seconds() -> None:
    runtime_config = resolve_review_runtime_config(_settings())
    assert runtime_config.request_timeout_seconds == 30.0


# -- B2: provider-aware model default (the actual production incident:
# PATCHFROG_REVIEW_PROVIDER=openai with PATCHFROG_REVIEW_MODEL left
# unset used to silently resolve to the Anthropic model name,
# "claude-opus-5" -- ModelRouter correctly selected openai, but the
# reviewer call 404'd against OpenAI's API with a claude model name) --


def test_openai_provider_with_model_unset_defaults_to_openai_model_not_anthropic() -> None:
    runtime_config = resolve_review_runtime_config(_settings(PATCHFROG_REVIEW_PROVIDER="openai"))
    assert runtime_config.provider == "openai"
    assert runtime_config.model == DEFAULT_MODEL_BY_PROVIDER["openai"]
    assert runtime_config.model != "claude-opus-5"


def test_gemini_provider_with_model_unset_defaults_to_gemini_model_not_anthropic() -> None:
    runtime_config = resolve_review_runtime_config(_settings(PATCHFROG_REVIEW_PROVIDER="gemini"))
    assert runtime_config.provider == "gemini"
    assert runtime_config.model == DEFAULT_MODEL_BY_PROVIDER["gemini"]
    assert runtime_config.model != "claude-opus-5"


def test_openai_provider_with_model_unset_critic_model_also_defaults_to_openai_model() -> None:
    runtime_config = resolve_review_runtime_config(_settings(PATCHFROG_REVIEW_PROVIDER="openai"))
    assert runtime_config.critic_model == DEFAULT_MODEL_BY_PROVIDER["openai"]


# -- B3: explicit model/provider pairing is validated -- an explicit,
# known-mismatched model must fail clearly, never silently reach a
# provider's API with the wrong model name --


def test_explicit_anthropic_model_with_openai_provider_raises_clearly() -> None:
    with pytest.raises(ValueError, match="does not look like a 'openai' model"):
        resolve_review_runtime_config(
            _settings(PATCHFROG_REVIEW_PROVIDER="openai", PATCHFROG_REVIEW_MODEL="claude-opus-5")
        )


def test_explicit_anthropic_model_with_gemini_provider_raises_clearly() -> None:
    with pytest.raises(ValueError, match="PATCHFROG_REVIEW_MODEL"):
        resolve_review_runtime_config(
            _settings(PATCHFROG_REVIEW_PROVIDER="gemini", PATCHFROG_REVIEW_MODEL="claude-opus-5")
        )


def test_explicit_mismatched_critic_model_raises_clearly() -> None:
    with pytest.raises(ValueError, match="PATCHFROG_REVIEW_CRITIC_MODEL"):
        resolve_review_runtime_config(
            _settings(
                PATCHFROG_REVIEW_PROVIDER="openai",
                PATCHFROG_REVIEW_MODEL="gpt-6",
                PATCHFROG_REVIEW_CRITIC_MODEL="claude-opus-5",
            )
        )


def test_explicit_matching_model_is_preserved_not_overridden() -> None:
    """Requirement: operator-controlled model configuration is honored
    exactly as set when it's valid for the configured provider -- never
    silently replaced by the provider's own default."""

    runtime_config = resolve_review_runtime_config(
        _settings(PATCHFROG_REVIEW_PROVIDER="openai", PATCHFROG_REVIEW_MODEL="gpt-6")
    )
    assert runtime_config.model == "gpt-6"


def test_models_resource_prefixed_model_name_is_normalized_before_validation() -> None:
    # See patchfrog.review.providers.gemini_provider's own docstring on
    # why a `models/`-prefixed resource-path form is a legitimate Gemini
    # model name, not a typo.
    runtime_config = resolve_review_runtime_config(
        _settings(PATCHFROG_REVIEW_PROVIDER="gemini", PATCHFROG_REVIEW_MODEL="models/gemini-3.6-flash")
    )
    assert runtime_config.model == "models/gemini-3.6-flash"


def test_unlisted_model_family_is_not_rejected() -> None:
    # Conservative by design: a model name matching neither known family
    # is never this check's business to reject -- only a model that
    # looks like a *different, known* provider's model is rejected.
    runtime_config = resolve_review_runtime_config(
        _settings(PATCHFROG_REVIEW_PROVIDER="openai", PATCHFROG_REVIEW_MODEL="some-future-model-name")
    )
    assert runtime_config.model == "some-future-model-name"


# -- C: Gemini runtime (provider + model set, nothing else) --


def test_gemini_provider_and_model_set_critic_defaults_to_reviewer_model() -> None:
    runtime_config = resolve_review_runtime_config(
        _settings(PATCHFROG_REVIEW_PROVIDER="gemini", PATCHFROG_REVIEW_MODEL="gemini-3.6-flash")
    )
    assert runtime_config.provider == "gemini"
    assert runtime_config.model == "gemini-3.6-flash"
    assert runtime_config.critic_model == "gemini-3.6-flash"


def test_gemini_provider_omitted_timeout_defaults_to_120_seconds() -> None:
    runtime_config = resolve_review_runtime_config(
        _settings(PATCHFROG_REVIEW_PROVIDER="gemini", PATCHFROG_REVIEW_MODEL="gemini-3.6-flash")
    )
    assert runtime_config.request_timeout_seconds == 120.0


def test_anthropic_provider_omitted_timeout_stays_30_seconds() -> None:
    runtime_config = resolve_review_runtime_config(
        _settings(PATCHFROG_REVIEW_PROVIDER="anthropic", PATCHFROG_REVIEW_MODEL="claude-opus-5")
    )
    assert runtime_config.request_timeout_seconds == 30.0


# -- D: explicit overrides always win over provider-appropriate defaults --


def test_explicit_critic_model_override_is_honored() -> None:
    runtime_config = resolve_review_runtime_config(
        _settings(
            PATCHFROG_REVIEW_PROVIDER="gemini",
            PATCHFROG_REVIEW_MODEL="gemini-3.6-flash",
            PATCHFROG_REVIEW_CRITIC_MODEL="some-other-valid-gemini-model",
        )
    )
    assert runtime_config.critic_model == "some-other-valid-gemini-model"


def test_explicit_timeout_override_is_honored() -> None:
    runtime_config = resolve_review_runtime_config(
        _settings(
            PATCHFROG_REVIEW_PROVIDER="gemini",
            PATCHFROG_REVIEW_MODEL="gemini-3.6-flash",
            PATCHFROG_REVIEW_REQUEST_TIMEOUT_SECONDS=60.0,
        )
    )
    assert runtime_config.request_timeout_seconds == 60.0


def test_explicit_critic_model_equal_to_default_reviewer_model_is_still_respected() -> None:
    """The distinction is "was this explicitly set", not "does it differ
    from the derived default" -- an operator may deliberately choose the
    exact string that also happens to match the reviewer model."""

    runtime_config = resolve_review_runtime_config(
        _settings(
            PATCHFROG_REVIEW_PROVIDER="anthropic",
            PATCHFROG_REVIEW_MODEL="claude-opus-5",
            PATCHFROG_REVIEW_CRITIC_MODEL="claude-opus-5",
        )
    )
    assert runtime_config.critic_model == "claude-opus-5"


# -- Validation --


def test_unsupported_provider_raises_clear_value_error() -> None:
    with pytest.raises(ValueError, match="unsupported PATCHFROG_REVIEW_PROVIDER"):
        resolve_review_runtime_config(_settings(PATCHFROG_REVIEW_PROVIDER="not-a-real-provider"))


def test_openai_is_a_supported_provider() -> None:
    config = resolve_review_runtime_config(_settings(PATCHFROG_REVIEW_PROVIDER="openai"))
    assert config.provider == "openai"


def test_non_positive_timeout_rejected_by_settings() -> None:
    with pytest.raises(ValueError, match="PATCHFROG_REVIEW_REQUEST_TIMEOUT_SECONDS must be positive"):
        _settings(PATCHFROG_REVIEW_REQUEST_TIMEOUT_SECONDS=0)


def test_negative_timeout_rejected_by_settings() -> None:
    with pytest.raises(ValueError, match="PATCHFROG_REVIEW_REQUEST_TIMEOUT_SECONDS must be positive"):
        _settings(PATCHFROG_REVIEW_REQUEST_TIMEOUT_SECONDS=-5)


# -- Canonical run identity: resolving runtime config never requires
# provider credentials (needed for a safe --dry-run, see patchfrog.cli).


def test_resolving_runtime_config_never_requires_credentials() -> None:
    runtime_config = resolve_review_runtime_config(
        _settings(PATCHFROG_REVIEW_PROVIDER="gemini", PATCHFROG_REVIEW_MODEL="gemini-3.6-flash")
    )
    assert runtime_config.provider == "gemini"


# -- H: CLI/worker parity -- both must resolve the same trusted runtime
# config for the same operator Settings, with no scattered/duplicated
# environment reads.


def test_cli_and_worker_import_the_same_resolver_function() -> None:
    """patchfrog.cli and apps.worker.tasks.review_pull_request must both
    call the exact same resolve_review_runtime_config function object --
    not two independently-maintained copies that could silently drift."""

    import apps.worker.tasks.review_pull_request as worker_task
    import patchfrog.cli as cli

    assert cli.resolve_review_runtime_config is resolve_review_runtime_config  # type: ignore[attr-defined]
    assert worker_task.resolve_review_runtime_config is resolve_review_runtime_config  # type: ignore[attr-defined]


def test_same_settings_resolve_identical_runtime_config_deterministically() -> None:
    settings = _settings(PATCHFROG_REVIEW_PROVIDER="gemini", PATCHFROG_REVIEW_MODEL="gemini-3.6-flash")
    first = resolve_review_runtime_config(settings)
    second = resolve_review_runtime_config(settings)
    assert first == second
