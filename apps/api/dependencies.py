"""FastAPI dependency providers.

Kept intentionally small for Phase 1: the webhook handler only needs
application settings (to verify signatures) — it does not touch the
database or the GitHub API directly, that work happens in the worker.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends

from patchfrog.config.settings import Settings, get_settings

SettingsDep = Annotated[Settings, Depends(get_settings)]
