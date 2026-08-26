"""Shared decode limits for the pure-Python convenience wrappers around
ultrajson's C decoder. Private beta validation sprint fixture -- not
part of the real ultrajson project."""

from __future__ import annotations

#: Maximum nesting depth ultrajson's C decoder is configured to accept
#: before raising. Every wrapper that pre-validates input before calling
#: the C decoder must use this constant, never a hardcoded literal, so
#: the two layers can never silently drift apart.
DEFAULT_MAX_DEPTH = 32
