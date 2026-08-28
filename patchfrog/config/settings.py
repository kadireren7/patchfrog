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
    # Same environment-only security model as anthropic_api_key above --
    # never read from .patchfrog.yml, never logged, never persisted.
    # Optional: patchfrog.review.provider_factory raises a clear,
    # actionable error only when provider="gemini" is actually requested.
    gemini_api_key: str = Field(default="", alias="GEMINI_API_KEY")

    # -- Public beta operational limits (patchfrog.ops) --
    # All optional with conservative defaults; never required for
    # startup, never a source of secrets. See docs/operations.md.

    #: When true, a newly `created` GitHub App installation starts in
    #: `BetaState.PENDING` (must be explicitly activated via
    #: `patchfrog ops installations activate`) instead of self-serve
    #: `BetaState.ACTIVE`. Off by default -- public self-serve beta.
    beta_allowlist_mode: bool = Field(default=False, alias="BETA_ALLOWLIST_MODE")
    #: Process-wide kill switch for scheduling *any* new review work
    #: (indexing/analysis/AI review) -- an emergency stop that takes
    #: effect on the next worker restart, never mid-request. Per-
    #: installation/per-repository switches (DB-persisted, no restart
    #: required) layer on top of this -- see `patchfrog.ops.eligibility`.
    global_review_processing_enabled: bool = Field(default=True, alias="GLOBAL_REVIEW_PROCESSING_ENABLED")
    #: Same as above, but for the publish stage specifically.
    global_publication_enabled: bool = Field(default=True, alias="GLOBAL_PUBLICATION_ENABLED")
    #: A PR with more changed files than this is skipped (RESOURCE_LIMIT,
    #: never processed partially) rather than attempting an unbounded
    #: review of a monorepo-sized change during beta.
    max_changed_files: int = Field(default=300, alias="MAX_CHANGED_FILES")
    #: Total diff size (sum of all changed files' patch text, bytes)
    #: above which a PR is skipped for the same reason.
    max_diff_bytes: int = Field(default=2_000_000, alias="MAX_DIFF_BYTES")
    #: Per-installation daily review count, used unless
    #: `InstallationModel.daily_review_limit` overrides it for one
    #: installation specifically.
    default_daily_review_limit: int = Field(default=50, alias="DEFAULT_DAILY_REVIEW_LIMIT")
    #: How many reviews may run concurrently for one installation --
    #: fairness, so one large/active installation can't starve every
    #: other beta user sharing the same worker pool.
    per_installation_concurrent_review_limit: int = Field(
        default=2, alias="PER_INSTALLATION_CONCURRENT_REVIEW_LIMIT"
    )
    #: A review run still `RUNNING` after this many minutes is
    #: considered stale (crashed worker, lost task) -- see
    #: `patchfrog ops stale`.
    stale_run_threshold_minutes: int = Field(default=60, alias="STALE_RUN_THRESHOLD_MINUTES")
    #: Port the worker container's own aggregating /metrics endpoint
    #: listens on -- only ever bound when `PROMETHEUS_MULTIPROC_DIR` is
    #: also set (see `patchfrog.ops.metrics`'s module docstring for why
    #: the API process's own /metrics can never see worker-incremented
    #: counters otherwise).
    worker_metrics_port: int = Field(default=9100, alias="WORKER_METRICS_PORT")

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
