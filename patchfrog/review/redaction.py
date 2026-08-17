"""Conservative secret redaction for text sent to an LLM provider.

Applied to every piece of repository-derived text before it leaves
PatchFrog (context snippets, diff excerpts, static-finding messages) --
never to the model's *response*, since a response can only reference what
was already redacted going in.

Deliberately conservative: this catches high-confidence, structurally
distinctive credential shapes (PEM private key blocks, GitHub tokens, AWS
access/secret keys, generic long high-entropy `KEY=value` assignments)
and nothing else. It must never fire on ordinary code identifiers,
hex/UUID literals, or long-but-ordinary strings -- a redaction layer that
mangles normal code teaches nobody to trust it and makes review output
worse without making anything safer. When genuinely unsure, prefer a
false negative (miss a secret) over corrupting legitimate code, and rely
on :mod:`patchfrog.review.candidates`' data-minimization (only
Context-Engine-selected snippets are ever sent, never `.env` files or
whole-repo dumps) as the primary defense.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_REDACTED = "[REDACTED]"

# Each pattern is deliberately narrow and structurally distinctive --
# comments explain exactly what real-world shape it targets.
_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "pem_private_key",
        re.compile(
            r"-----BEGIN (?:RSA |EC |OPENSSH |DSA |ENCRYPTED )?PRIVATE KEY-----"
            r".*?"
            r"-----END (?:RSA |EC |OPENSSH |DSA |ENCRYPTED )?PRIVATE KEY-----",
            re.DOTALL,
        ),
    ),
    # GitHub fine-grained/classic PAT and App tokens: fixed, unmistakable
    # prefixes followed by a long base62-ish body.
    ("github_token", re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr|github_pat)_[A-Za-z0-9_]{20,}\b")),
    # AWS access key id: exact fixed-width, fixed-prefix shape.
    ("aws_access_key_id", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    # AWS secret access key: only redacted when explicitly labeled by a
    # recognizable assignment -- a bare 40-char base64-ish string alone is
    # indistinguishable from a hash or ordinary token and would be far too
    # aggressive to blanket-redact.
    (
        "aws_secret_access_key",
        re.compile(
            r"(?i)\b(?:aws_secret_access_key|aws-secret-access-key)\s*[:=]\s*"
            r"['\"]?([A-Za-z0-9/+=]{40})['\"]?"
        ),
    ),
    # Slack bot/app tokens.
    ("slack_token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b")),
    # Generic `SOMETHING_KEY = "<long high-entropy value>"` assignment --
    # the label must actually say key/secret/token/password, and the
    # value must be long enough that redacting it can't plausibly clip a
    # short, ordinary code literal.
    (
        "labeled_secret_assignment",
        re.compile(
            r"(?i)\b([A-Z0-9_]*(?:SECRET|API_KEY|APIKEY|PASSWORD|PRIVATE_KEY|ACCESS_TOKEN|AUTH_TOKEN)"
            r"[A-Z0-9_]*)\s*[:=]\s*['\"]([A-Za-z0-9/+._=-]{16,})['\"]"
        ),
    ),
)


@dataclass(frozen=True, slots=True)
class RedactionResult:
    text: str
    redacted_count: int
    redacted_kinds: tuple[str, ...]


def redact_secrets(text: str) -> RedactionResult:
    """Replace every high-confidence secret-shaped substring in ``text``
    with a fixed placeholder. Idempotent and order-independent across the
    pattern set (patterns target disjoint, structurally distinctive
    shapes, so overlap is not a practical concern)."""

    kinds: list[str] = []
    result = text

    for kind, pattern in _PATTERNS:
        if kind in ("aws_secret_access_key", "labeled_secret_assignment"):
            new_result, count = pattern.subn(
                lambda m: m.group(0).replace(m.group(m.lastindex or 0), _REDACTED), result
            )
        else:
            new_result, count = pattern.subn(_REDACTED, result)
        if count:
            kinds.extend([kind] * count)
        result = new_result

    return RedactionResult(text=result, redacted_count=len(kinds), redacted_kinds=tuple(kinds))
