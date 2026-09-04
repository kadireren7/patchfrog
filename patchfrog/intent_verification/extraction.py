"""Deterministic intent extraction and the sufficiency gate (spec
section 5) -- this decision is never delegated to an LLM.

No semantic parsing, no NLP model. A claim's ``normalized_statement`` is
the sanitized, whitespace-collapsed, bounded source text itself (spec
section 6) -- never a paraphrase, never an LLM summary.
"""

from __future__ import annotations

import hashlib
import re

from patchfrog.intent_verification.domain import (
    MAX_INTENT_CLAIMS,
    IntentClaim,
    IntentEvidence,
    IntentSourceKind,
    IntentStrength,
)
from patchfrog.publishing.marker import sanitize_untrusted_text

#: Never persist/forward more than this many characters of PR title/body
#: text -- bounded, matching every other engine's evidence-text
#: discipline in this codebase.
MAX_BOUNDED_TEXT_CHARS = 500

#: A normalized statement shorter than this (in real "content words",
#: after stopword removal) is never sufficient on its own, even with a
#: recognized behavioral verb present -- spec section 5's own examples
#: ("fix", "cleanup", "changes", "WIP", "refactor stuff", "try again")
#: are all this short.
_MIN_CONTENT_WORDS_WITH_VERB = 3
#: Without a recognized verb, demand substantially more content to
#: compensate -- avoids requiring an exhaustive verb dictionary while
#: still rejecting short, vague, purely nominal text.
_MIN_CONTENT_WORDS_WITHOUT_VERB = 7

#: Exact-match placeholder/vague statements (spec section 5's own list,
#: plus obvious variants) -- checked against the *entire* normalized
#: statement, never a substring, so a real sentence that happens to
#: contain the word "fix" is never penalized.
_VAGUE_PLACEHOLDERS = frozenset(
    {
        "fix", "fixes", "fixed", "fixing",
        "cleanup", "clean up", "cleanups",
        "changes", "change", "misc", "misc changes", "miscellaneous",
        "wip", "work in progress",
        "update", "updates", "updated", "updating",
        "refactor", "refactors", "refactoring", "refactor stuff",
        "try again", "retry", "tweak", "tweaks", "chore", "chores",
        "stuff", "things", "minor fix", "small fix", "quick fix",
        "test", "testing", "tests", "todo", "temp", "temporary",
    }
)

#: A small, curated set of behavioral verbs commonly used to state real
#: intent -- deliberately not exhaustive (see
#: `_MIN_CONTENT_WORDS_WITHOUT_VERB` above for how prose without one of
#: these is still evaluated).
_BEHAVIORAL_VERBS = frozenset(
    {
        "prevent", "prevents", "prevented", "preventing",
        "ensure", "ensures", "ensured", "ensuring",
        "reject", "rejects", "rejected", "rejecting",
        "allow", "allows", "allowed", "allowing",
        "support", "supports", "supported", "supporting",
        "handle", "handles", "handled", "handling",
        "enforce", "enforces", "enforced", "enforcing",
        "block", "blocks", "blocked", "blocking",
        "require", "requires", "required", "requiring",
        "restrict", "restricts", "restricted", "restricting",
        "validate", "validates", "validated", "validating",
        "correct", "corrects", "corrected", "correcting",
        "resolve", "resolves", "resolved", "resolving",
        "stop", "stops", "stopped", "stopping",
        "disallow", "disallows", "disallowed",
        "guarantee", "guarantees", "guaranteed",
        "expose", "exposes", "exposed", "exposing",
        "cache", "cached", "caching",
        "expire", "expires", "expired", "expiring",
        "invalidate", "invalidates", "invalidated",
        "sanitize", "sanitizes", "sanitized",
        "redirect", "redirects", "redirected",
        "authenticate", "authenticates", "authenticated",
        "authorize", "authorizes", "authorized",
        "throttle", "throttles", "throttled",
        "deduplicate", "deduplicates", "deduplicated", "dedupe",
        "limit", "limits", "limited", "limiting",
        "reset", "resets",
        "rotate", "rotates", "rotated",
        "queue", "queues", "queued",
        "process", "processes", "processed", "processing",
        "create", "creates", "created", "creating",
        "make", "makes", "making",
        "add", "adds", "adding",
        "remove", "removes", "removing",
        "delete", "deletes", "deleting",
        "improve", "improves", "improving",
        "implement", "implements", "implementing",
        "introduce", "introduces", "introducing",
        "migrate", "migrates", "migrating",
        "retries", "retrying",
    }
)

_STOPWORDS = frozenset(
    {
        "a", "an", "the", "this", "that", "these", "those", "to", "for",
        "of", "in", "on", "and", "or", "is", "are", "was", "were", "be",
        "been", "being", "it", "its", "with", "by", "as", "at", "so",
        "also", "now", "just", "should", "would", "could", "will",
        "can", "may", "might", "must", "pr", "when", "if", "not", "no",
    }
)

_WORD_RE = re.compile(r"[a-z0-9']+")


def normalize_intent_text(text: str) -> str:
    """Whitespace-collapse, strip, and bound -- the *only* transformation
    ever applied to raw PR title/body text (never an LLM paraphrase)."""

    collapsed = " ".join(text.strip().split())
    sanitized = sanitize_untrusted_text(collapsed)
    return sanitized[:MAX_BOUNDED_TEXT_CHARS]


def is_intent_evidence_sufficient(text: str) -> bool:
    """The deterministic usability gate (spec section 5) -- fails closed
    on vague/placeholder/too-short text. Never delegated to an LLM."""

    normalized = " ".join(text.strip().split()).lower()
    if not normalized:
        return False
    if normalized in _VAGUE_PLACEHOLDERS:
        return False

    words = _WORD_RE.findall(normalized)
    content_words = [w for w in words if w not in _STOPWORDS]
    has_verb = any(w in _BEHAVIORAL_VERBS for w in words)

    threshold = _MIN_CONTENT_WORDS_WITH_VERB if has_verb else _MIN_CONTENT_WORDS_WITHOUT_VERB
    return len(content_words) >= threshold


def extract_claims_from_pr_metadata(*, title: str | None, body: str | None) -> tuple[IntentClaim, ...]:
    """Explicit claims from PR title/body (spec sections 2/3/6/7).

    Title and body are evaluated independently -- a sufficient title
    always yields a claim even with a vague/empty body, and vice versa
    (spec section 29 cases 12/13). Bounded to
    :data:`~patchfrog.intent_verification.domain.MAX_INTENT_CLAIMS`,
    though in practice only ever produces at most 2 (one per source)
    -- the bound exists for forward-compatibility with a future,
    still-deterministic multi-statement body split, not exercised by
    this milestone's conservative single-claim-per-source extraction
    (spec section 7: "Otherwise prefer one conservative combined
    claim.")."""

    claims: list[IntentClaim] = []

    if title:
        normalized = normalize_intent_text(title)
        if is_intent_evidence_sufficient(normalized):
            claims.append(_build_claim(source_kind=IntentSourceKind.PR_TITLE, source_identifier="title", text=normalized))

    if body:
        normalized = normalize_intent_text(body)
        if is_intent_evidence_sufficient(normalized):
            claims.append(_build_claim(source_kind=IntentSourceKind.PR_BODY, source_identifier="body", text=normalized))

    return tuple(claims[:MAX_INTENT_CLAIMS])


def _build_claim(*, source_kind: IntentSourceKind, source_identifier: str, text: str) -> IntentClaim:
    evidence = IntentEvidence(
        source_kind=source_kind, source_identifier=source_identifier, bounded_text=text,
        strength=IntentStrength.EXPLICIT,
    )
    claim_id = hashlib.sha256(f"{source_kind.value}\x1f{text}".encode()).hexdigest()[:16]
    return IntentClaim(id=claim_id, normalized_statement=text, source=evidence, strength=IntentStrength.EXPLICIT)
