"""Deterministic token-count approximation.

No external tokenizer dependency -- a documented, simple
characters-divided-by-four estimate, which is the commonly cited rough
ratio for English/code text and good enough for a *budget*, not a billing
figure. Kept in one place so every component estimates the same way.
"""

from __future__ import annotations

_CHARS_PER_TOKEN_ESTIMATE = 4


def estimate_tokens(text: str) -> int:
    if not text:
        return 0
    return max(1, (len(text) + _CHARS_PER_TOKEN_ESTIMATE - 1) // _CHARS_PER_TOKEN_ESTIMATE)
