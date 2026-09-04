"""Shared deterministic tokenization for lexical-relevance matching
(spec section 8: "bounded lexical overlap... Do NOT use embedding
infrastructure... Do NOT create a vector database.").

Splits identifiers/paths into real subword tokens (snake_case,
camelCase, path separators, extensions) so an intent term like
``duplicate`` matches a symbol like ``is_duplicate_payment`` or a file
like ``duplicate_guard.py`` -- never a fuzzy/similarity match, only
exact-token-after-splitting equality.
"""

from __future__ import annotations

import re

#: Below this length a token is too generic to count as a meaningful
#: match on its own (spec section 8: "If mapping is ambiguous: leave
#: the claim unmapped") -- avoids spurious overlap on short, common
#: words like "id"/"of"/"is".
MIN_MEANINGFUL_TOKEN_LENGTH = 4

_CAMEL_BOUNDARY_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_NON_ALNUM_RE = re.compile(r"[^a-zA-Z0-9]+")

_STOPWORDS = frozenset(
    {
        "a", "an", "the", "this", "that", "these", "those", "to", "for",
        "of", "in", "on", "and", "or", "is", "are", "was", "were", "be",
        "been", "being", "it", "its", "with", "by", "as", "at", "so",
        "also", "now", "just", "should", "would", "could", "will",
        "can", "may", "might", "must", "pr", "when", "if", "not", "no",
        "py", "test", "tests",
    }
)


def tokenize(text: str) -> frozenset[str]:
    """Splits ``text`` (prose, an identifier, or a file path) into
    lowercase subword tokens -- snake_case and camelCase boundaries,
    path separators, punctuation, and file extensions all split."""

    spaced = _CAMEL_BOUNDARY_RE.sub(" ", text)
    spaced = _NON_ALNUM_RE.sub(" ", spaced)
    return frozenset(w.lower() for w in spaced.split() if w)


def meaningful_tokens(text: str) -> frozenset[str]:
    """:func:`tokenize`, filtered to non-stopword tokens long enough to
    be a specific, non-generic match."""

    return frozenset(
        t for t in tokenize(text) if t not in _STOPWORDS and len(t) >= MIN_MEANINGFUL_TOKEN_LENGTH
    )
