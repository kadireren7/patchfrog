"""Structured logging configuration.

Called once at process startup by the API app and the Celery worker.
Every log call site attaches contextual fields (delivery id, repository,
PR number, ...) instead of formatting them into free-text messages.
"""

from __future__ import annotations

import logging
import re
import sys
from collections.abc import MutableMapping
from typing import Any

import structlog

#: Field *names* that, if a call site ever attached one (never expected
#: today -- see the module docstring of :mod:`patchfrog.config.settings`
#: on credentials always staying environment-only, never logged), get
#: their value replaced outright. Matched case-insensitively against a
#: normalized (underscores/hyphens stripped) key, so `github_private_key`,
#: `Authorization`, and `api-key` are all caught by one entry each.
_SECRET_KEY_PATTERNS = (
    "privatekey",
    "installationtoken",
    "webhooksecret",
    "apikey",
    "authorization",
    "password",
    "secret",
    "token",
)

#: Key *suffixes* checked in addition to the exact-match set above --
#: `anthropic_api_key`, `openai_api_key`, etc. all normalize to something
#: ending in `apikey` but aren't caught by an exact-match lookup against
#: the bare `apikey` entry. Deliberately a suffix check, not a substring
#: check: a substring check on the generic `token` entry above would also
#: redact legitimate, non-secret telemetry fields this codebase actually
#: logs today (`reviewer_input_tokens`, `critic_output_tokens`, ...).
_SECRET_KEY_SUFFIXES = ("apikey",)

#: Value *shapes* redacted regardless of which field they showed up
#: under -- catches a secret accidentally interpolated into a free-text
#: message or an unexpected field name this processor's key-name list
#: doesn't yet cover.
_PEM_BLOCK = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.DOTALL)
_BEARER_TOKEN = re.compile(r"Bearer\s+[A-Za-z0-9._\-]+")
_GITHUB_TOKEN = re.compile(r"gh[a-z]_[A-Za-z0-9]{20,}")
_ANTHROPIC_TOKEN = re.compile(r"sk-ant-[A-Za-z0-9_\-]{20,}")

_REDACTED = "***redacted***"


def _normalize_key(key: str) -> str:
    return re.sub(r"[_\-\s]", "", key).lower()


def _redact_value(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    value = _PEM_BLOCK.sub(_REDACTED, value)
    value = _BEARER_TOKEN.sub(_REDACTED, value)
    value = _GITHUB_TOKEN.sub(_REDACTED, value)
    value = _ANTHROPIC_TOKEN.sub(_REDACTED, value)
    return value


def redact_secrets(
    _logger: Any, _method_name: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    """Deterministic, defense-in-depth log redaction (spec section 4).
    Never relied upon as the *only* safeguard -- no call site in this
    codebase attaches a raw secret to a log call in the first place (see
    :mod:`patchfrog.config.settings`) -- but a processor that always runs
    catches a future mistake before it reaches stdout, rather than after."""

    for key, value in list(event_dict.items()):
        normalized = _normalize_key(key)
        if normalized in _SECRET_KEY_PATTERNS or normalized.endswith(_SECRET_KEY_SUFFIXES):
            event_dict[key] = _REDACTED
        else:
            event_dict[key] = _redact_value(value)
    return event_dict


def configure_logging(log_level: str) -> None:
    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=getattr(logging, log_level, logging.INFO),
    )

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            redact_secrets,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, log_level, logging.INFO)
        ),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )
