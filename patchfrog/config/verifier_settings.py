"""Verifier-only configuration -- Milestone S6.

Deliberately a **separate, narrower** settings class from
:class:`patchfrog.config.settings.Settings`, not an extension of it.
``Settings`` requires ``DATABASE_URL``, ``GITHUB_APP_ID``,
``GITHUB_PRIVATE_KEY``/``_PATH``, and ``GITHUB_WEBHOOK_SECRET`` at
construction time -- any process that imports it (including, transitively,
:mod:`apps.worker.celery_app`, which calls ``get_settings()`` at module
import time) must already have every one of those in its environment just
to start, whether or not it uses them. A verifier process that is meant to
hold *none* of those credentials cannot be built on top of that class.

``VerifierSettings`` needs exactly one thing to do its job: a Redis URL,
to build its own Celery app sharing the same broker/result backend the
review worker already uses (see
:mod:`patchfrog.executable_verification.protocol` for the queue/task
name constants that keep the two apps talking to each other without
either importing the other's task modules). No database, no GitHub
credential, no LLM provider credential, no webhook secret -- see
``validation/production_execution/latest-summary.md`` Part I for the full
credential-boundary rationale.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class VerifierSettings(BaseSettings):
    """Application settings for the verifier process only."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_env: str = Field(default="development", alias="APP_ENV")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")

    #: The verifier SERVICE process's own communication credential -- it
    #: consumes its own dedicated queue (never the review worker's own
    #: task queues) and shares only the Celery result backend, never any
    #: other Redis-backed state. This credential is never passed into the
    #: hostile sandboxed test subprocess (see
    #: patchfrog.analysis.subprocess_sandbox.sandboxed_env's own strict
    #: allowlist, unchanged by this milestone).
    redis_url: str = Field(alias="REDIS_URL")

    #: Security correction (Part 5): the one operator-owned root directory
    #: the verifier will ever read a staged artifact from. A request's own
    #: `artifact_id` is *never* a raw filesystem path -- it is resolved as
    #: `(staging_root / artifact_id).resolve()` and rejected (SANDBOX_ERROR)
    #: unless the result is still beneath this exact root (see
    #: apps.verifier.tasks.resolve_artifact_path). Required, no default:
    #: an operator deploying the verifier must explicitly decide where its
    #: one trusted read location is -- it can never be inferred from a
    #: request, and never from repository-controlled configuration.
    staging_root: str = Field(alias="VERIFIER_STAGING_ROOT")

    @field_validator("log_level")
    @classmethod
    def _validate_log_level(cls, value: str) -> str:
        normalized = value.upper()
        allowed = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        if normalized not in allowed:
            raise ValueError(f"LOG_LEVEL must be one of {sorted(allowed)}, got {value!r}")
        return normalized

    def __repr__(self) -> str:
        # Never let a settings object leak secrets (here: the Redis URL,
        # which may embed a password) through logs/tracebacks.
        return f"VerifierSettings(app_env={self.app_env!r}, log_level={self.log_level!r})"

    __str__ = __repr__


@lru_cache(maxsize=1)
def get_verifier_settings() -> VerifierSettings:
    """Return the process-wide cached verifier settings instance."""

    return VerifierSettings()
