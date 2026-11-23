"""Phase 1 (ADR-011): local ``CoreSettings`` vs. server ``Settings``.

Two properties are protected here:

1. ``CoreSettings`` constructs with a completely empty environment -- the
   local-first guarantee.
2. The server ``Settings`` environment contract (every alias, and exactly
   which aliases are required) is unchanged. Self-hosted deployments and
   the private PatchFrog Cloud integration configure the engine through
   these names; ``test_server_settings_env_contract_is_unchanged`` is the
   explicit compatibility check required before any change to this
   Cloud-consumed surface (ADR-015). Changing the snapshot below is a
   deliberate, coordinated contract change, never a drive-by update.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from patchfrog.config.core import CoreSettings, get_core_settings
from patchfrog.config.settings import ServerSettings, Settings

_REQUIRED_SERVER_ALIASES = frozenset({"DATABASE_URL", "GITHUB_APP_ID", "GITHUB_WEBHOOK_SECRET", "REDIS_URL"})

_ALL_SERVER_ALIASES = frozenset(
    {
        "ANTHROPIC_API_KEY", "APP_ENV", "BETA_ALLOWLIST_MODE", "DATABASE_URL",
        "DEFAULT_DAILY_REVIEW_LIMIT", "GEMINI_API_KEY", "GITHUB_API_BASE_URL",
        "GITHUB_API_TIMEOUT_SECONDS", "GITHUB_APP_ID", "GITHUB_PRIVATE_KEY",
        "GITHUB_PRIVATE_KEY_PATH", "GITHUB_WEBHOOK_SECRET", "GLOBAL_PUBLICATION_ENABLED",
        "GLOBAL_REVIEW_PROCESSING_ENABLED", "LOG_LEVEL", "MAX_CHANGED_FILES", "MAX_DIFF_BYTES",
        "OPENAI_API_KEY", "PATCHFROG_ALLOWED_PROVIDERS", "PATCHFROG_CRITIC_FAILURE_POLICY",
        "PATCHFROG_MAX_CONCURRENT_REVIEW_REQUESTS", "PATCHFROG_MAX_ESTIMATED_COST_USD",
        "PATCHFROG_MAX_OUTPUT_TOKENS_PER_CANDIDATE", "PATCHFROG_MAX_PROVIDER_CALLS",
        "PATCHFROG_MAX_RETRY_ATTEMPTS", "PATCHFROG_MAX_REVIEW_CANDIDATES",
        "PATCHFROG_MAX_REVIEW_ELAPSED_SECONDS", "PATCHFROG_MAX_REVIEW_RETRIES",
        "PATCHFROG_MAX_TOTAL_INPUT_TOKENS", "PATCHFROG_MAX_TOTAL_OUTPUT_TOKENS",
        "PATCHFROG_PROVIDER_PRICING", "PATCHFROG_PROVIDER_RATE_LIMIT_RPM",
        "PATCHFROG_REVIEW_CRITIC_MODEL", "PATCHFROG_REVIEW_EXCLUDE_GENERATED_AND_VENDOR",
        "PATCHFROG_REVIEW_MODEL", "PATCHFROG_REVIEW_PROVIDER",
        "PATCHFROG_REVIEW_REQUEST_TIMEOUT_SECONDS", "PATCHFROG_REVIEW_STRATEGY",
        "PATCHFROG_RISK_TIER_MAX_PROVIDER_CALLS", "PATCHFROG_ROUTER_CHEAP_MODEL",
        "PATCHFROG_ROUTER_CHEAP_PROVIDER", "PATCHFROG_ROUTER_CRITIC_PROVIDER",
        "PATCHFROG_ROUTER_FALLBACK_PROVIDER", "PATCHFROG_VERIFIER_ENABLED",
        "PATCHFROG_VERIFIER_WAIT_TIMEOUT_SECONDS", "PER_INSTALLATION_CONCURRENT_REVIEW_LIMIT",
        "REDIS_URL", "STALE_RUN_THRESHOLD_MINUTES", "VERIFICATION_SNAPSHOT_ROOT",
        "WORKER_METRICS_PORT",
    }
)


def _clear_server_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for alias in _ALL_SERVER_ALIASES:
        monkeypatch.delenv(alias, raising=False)


def test_core_settings_constructs_with_empty_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_server_environment(monkeypatch)

    settings = CoreSettings(_env_file=None)

    assert settings.app_env == "development"
    assert settings.log_level == "INFO"


def test_core_settings_reads_its_own_environment_variables(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_server_environment(monkeypatch)
    monkeypatch.setenv("APP_ENV", "local")
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")

    settings = CoreSettings(_env_file=None)

    assert (settings.app_env, settings.log_level) == ("local", "DEBUG")


def test_core_settings_has_no_required_fields() -> None:
    assert [name for name, field in CoreSettings.model_fields.items() if field.is_required()] == []


def test_get_core_settings_is_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_server_environment(monkeypatch)
    get_core_settings.cache_clear()
    try:
        assert get_core_settings() is get_core_settings()
    finally:
        get_core_settings.cache_clear()


def test_server_settings_is_a_strict_superset_of_core_settings() -> None:
    assert ServerSettings is Settings
    assert issubclass(Settings, CoreSettings)
    for name, core_field in CoreSettings.model_fields.items():
        server_field = Settings.model_fields[name]
        assert server_field.alias == core_field.alias
        assert server_field.default == core_field.default


def test_server_settings_env_contract_is_unchanged() -> None:
    fields = Settings.model_fields.values()

    assert frozenset(f.alias for f in fields) == _ALL_SERVER_ALIASES
    assert frozenset(f.alias for f in fields if f.is_required()) == _REQUIRED_SERVER_ALIASES


def test_server_settings_accepts_mixed_field_name_and_alias_construction(
    monkeypatch: pytest.MonkeyPatch, test_private_key: str
) -> None:
    """Downstream integrations (verified 2026-11-22 for a real consumer)
    build ``Settings`` in one call with *field names* for the core server
    fields and *aliases* for the review-routing fields. ``Settings`` is part
    of the Public Engine API v0 (docs/architecture/PUBLIC_API.md); this pins
    that the mixed shape keeps validating and populates the intended
    fields."""

    _clear_server_environment(monkeypatch)

    settings = Settings(
        _env_file=None,
        database_url="postgresql+asyncpg://u:p@db/x",
        redis_url="redis://r:6379/0",
        github_app_id="42",
        github_private_key=test_private_key,
        github_webhook_secret="whs",
        github_api_base_url="https://api.github.example",
        github_api_timeout_seconds=7.5,
        anthropic_api_key="",
        gemini_api_key="g-key",
        openai_api_key="",
        PATCHFROG_REVIEW_PROVIDER="gemini",
        PATCHFROG_REVIEW_MODEL="gemini-x",
        PATCHFROG_REVIEW_CRITIC_MODEL="gemini-y",
        PATCHFROG_ROUTER_FALLBACK_PROVIDER=None,
        PATCHFROG_ROUTER_CRITIC_PROVIDER=None,
    )

    assert settings.database_url == "postgresql+asyncpg://u:p@db/x"
    assert settings.github_app_id == "42"
    assert settings.github_api_timeout_seconds == 7.5
    assert settings.review_provider == "gemini"
    assert settings.review_model == "gemini-x"
    assert settings.review_critic_model == "gemini-y"
    assert isinstance(settings, CoreSettings)
    assert settings.app_env == "development"


def test_server_settings_still_fails_without_server_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_server_environment(monkeypatch)

    with pytest.raises(ValidationError) as exc_info:
        Settings(_env_file=None)

    missing = {str(error["loc"][0]) for error in exc_info.value.errors() if error["type"] == "missing"}
    assert missing == _REQUIRED_SERVER_ALIASES


def test_server_settings_repr_still_redacts_secrets(test_private_key: str) -> None:
    settings = Settings(
        _env_file=None,
        DATABASE_URL="postgresql+asyncpg://user:db-password@localhost/db",
        REDIS_URL="redis://localhost:6379/0",
        GITHUB_APP_ID="1",
        GITHUB_PRIVATE_KEY=test_private_key,
        GITHUB_WEBHOOK_SECRET="super-secret-value",
    )

    rendered = repr(settings)

    assert rendered.startswith("Settings(")
    for secret in ("super-secret-value", "db-password", "PRIVATE KEY"):
        assert secret not in rendered
