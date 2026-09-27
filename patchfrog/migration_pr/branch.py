"""Deterministic migration branch naming (M9.2).

    patchfrog/migrate/<provider>/<change-short-id>

Stable for the same migration identity (same provider, same upstream
change fingerprint) -- a re-run reconciles the same branch rather than
creating a new one (M9.6). ``provider`` is sanitized to a safe, git-ref-
legal token; nothing here is ever interpolated into a shell command (the
branch is only ever used as a JSON field in a GitHub API request body --
see :mod:`patchfrog.github.client`), so there is no command-injection
surface, only a git-ref-syntax one, which this still guards defensively.
"""

from __future__ import annotations

import re

_UNSAFE_REF_CHARS = re.compile(r"[^a-z0-9._-]+")
_SHORT_ID_LENGTH = 12


def _sanitize(token: str) -> str:
    safe = _UNSAFE_REF_CHARS.sub("-", token.lower()).strip("-.")
    return safe or "unknown"


def migration_branch_name(*, provider: str | None, change_fingerprint: str) -> str:
    """``provider`` may be ``None`` (no explicit provider identity was
    ever supplied for this upstream change) -- falls back to a stable
    ``"dependency"`` token rather than omitting the segment, so the
    branch shape is always exactly three path segments."""

    safe_provider = _sanitize(provider) if provider else "dependency"
    short_id = change_fingerprint[:_SHORT_ID_LENGTH] or "0" * _SHORT_ID_LENGTH
    return f"patchfrog/migrate/{safe_provider}/{short_id}"


__all__ = ["migration_branch_name"]
