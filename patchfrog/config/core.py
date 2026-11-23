"""Core (local-mode) configuration -- Phase 1, ADR-011.

The minimal settings any PatchFrog process needs, with **no required
fields**: no database, Redis, GitHub App identity, network service, or AI
provider credential. A local command, the local MCP surface, and every
future local-first entry point construct this, never
:class:`patchfrog.config.settings.Settings`.

:class:`patchfrog.config.settings.Settings` (the server/self-hosted
configuration, also exported as ``ServerSettings``) subclasses this class,
so the server configuration stays a strict superset: every core field
keeps the same name, environment alias, and default in both, and a
``Settings`` instance is accepted anywhere a ``CoreSettings`` is.

Deliberately dependency-free within PatchFrog (no import of any feature
package such as ``patchfrog.review``): importing this module must never
pull in the server stack. ``tests/unit/test_architecture_boundaries.py``
enforces that.

Adding a field here is a contract change for both modes: it must have a
safe default (local mode must keep working with an empty environment).
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class CoreSettings(BaseSettings):
    """Settings every PatchFrog process may read; none are required."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_env: str = Field(default="development", alias="APP_ENV")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")

    def __repr__(self) -> str:
        return f"{type(self).__name__}(app_env={self.app_env!r}, log_level={self.log_level!r})"

    __str__ = __repr__


@lru_cache(maxsize=1)
def get_core_settings() -> CoreSettings:
    """Return the process-wide cached core settings instance.

    Never raises for a missing environment variable -- by construction,
    every field has a default.
    """

    return CoreSettings()
