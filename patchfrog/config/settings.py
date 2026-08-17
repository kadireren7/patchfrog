"""Typed, environment-driven application configuration.

All runtime configuration flows through a single :class:`Settings` instance.
Nothing else in the codebase should read from ``os.environ`` directly.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings loaded from environment variables / .env file."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_env: str = Field(default="development", alias="APP_ENV")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")

    database_url: str = Field(alias="DATABASE_URL")
    redis_url: str = Field(alias="REDIS_URL")

    github_app_id: str = Field(alias="GITHUB_APP_ID")
    # Exactly one of these two must be set. GITHUB_PRIVATE_KEY_PATH is
    # preferred (keeps a multiline PEM out of the process environment and
    # out of .env); GITHUB_PRIVATE_KEY (inline PEM) is kept for simplicity
    # in constrained environments (e.g. some PaaS secret managers only
    # support single env vars). Resolved into `github_private_key` below.
    github_private_key: str = Field(default="", alias="GITHUB_PRIVATE_KEY")
    github_private_key_path: str | None = Field(default=None, alias="GITHUB_PRIVATE_KEY_PATH")
    github_webhook_secret: str = Field(alias="GITHUB_WEBHOOK_SECRET")

    github_api_base_url: str = Field(
        default="https://api.github.com", alias="GITHUB_API_BASE_URL"
    )
    github_api_timeout_seconds: float = Field(
        default=10.0, alias="GITHUB_API_TIMEOUT_SECONDS"
    )

    # AI Reviewer (Phase 5): credentials for the configured LLM provider.
    # Environment-only -- never read from .patchfrog.yml (see
    # patchfrog.review.config.load_review_config), never logged, never
    # persisted. Optional: if unset, patchfrog.review.provider_factory
    # raises a clear, actionable error only when a real provider is
    # actually requested (CLI --dry-run and the Celery task's own
    # provider-construction step both handle this explicitly).
    anthropic_api_key: str = Field(default="", alias="ANTHROPIC_API_KEY")

    @field_validator("log_level")
    @classmethod
    def _validate_log_level(cls, value: str) -> str:
        normalized = value.upper()
        allowed = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        if normalized not in allowed:
            raise ValueError(f"LOG_LEVEL must be one of {sorted(allowed)}, got {value!r}")
        return normalized

    @model_validator(mode="after")
    def _resolve_private_key(self) -> Settings:
        inline, path = self.github_private_key, self.github_private_key_path

        if inline and path:
            raise ValueError(
                "Set only one of GITHUB_PRIVATE_KEY or GITHUB_PRIVATE_KEY_PATH, not both"
            )

        if path:
            try:
                inline = Path(path).read_text()
            except OSError as exc:
                raise ValueError(f"Could not read GITHUB_PRIVATE_KEY_PATH file: {path}") from exc

        if not inline:
            raise ValueError(
                "Set GITHUB_PRIVATE_KEY (inline PEM) or "
                "GITHUB_PRIVATE_KEY_PATH (path to a PEM file)"
            )

        inline = inline.replace("\\n", "\n")
        if "BEGIN" not in inline:
            raise ValueError(
                "GITHUB_PRIVATE_KEY must be a PEM-encoded private key "
                "(use \\n for newlines if set as a single-line env var)"
            )

        self.github_private_key = inline
        return self

    def __repr__(self) -> str:
        # Never let a settings object leak secrets through logs/tracebacks.
        return (
            f"Settings(app_env={self.app_env!r}, log_level={self.log_level!r}, "
            f"github_app_id={self.github_app_id!r})"
        )

    __str__ = __repr__


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide cached settings instance."""

    return Settings()
