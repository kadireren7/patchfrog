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

#: A deterministic, structural (never NLP) detector for an explicitly
#: enumerated list of goals in a PR body -- a markdown bullet (``-``/``*``)
#: or numbered (``1.``) line start. Splitting on this is "reliable
#: deterministic extraction" in the sense spec section 7 requires before
#: preserving more than one claim from a single source; prose without
#: this structure is never split (see `_extract_enumerated_goals`).
_BULLET_LINE_RE = re.compile(r"^\s*(?:[-*]|\d+[.)])\s+(.+)$")


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


def _extract_enumerated_goals(body: str) -> list[str] | None:
    """Deterministic, structure-only (never NLP) detection of an
    explicitly enumerated list of goals in a PR body -- markdown bullet
    or numbered lines, checked *before* whitespace-collapsing destroys
    the line boundaries a bullet list depends on. Returns ``None`` (not
    an empty list) whenever fewer than 2 bullet lines are found, so a
    single stray ``- `` in otherwise-prose body text never triggers a
    split."""

    bullets = [m.group(1).strip() for line in body.splitlines() if (m := _BULLET_LINE_RE.match(line))]
    return bullets if len(bullets) >= 2 else None


def extract_claims_from_pr_metadata(*, title: str | None, body: str | None) -> tuple[IntentClaim, ...]:
    """Explicit claims from PR title/body (spec sections 2/3/6/7), with a
    single, deterministic, documented precedence rule for when both are
    independently usable (spec section "title/body contradiction"):
    **the PR body is authoritative whenever it is itself sufficient
    evidence; the title is used only as a fallback when the body is
    absent or insufficient.** This is a structural precedence policy,
    never semantic contradiction detection (which would require
    guessing) -- it also means title and body can never simultaneously
    produce two separate, potentially-conflicting claims for the same
    PR (spec section "multiple claims": "do not emit duplicate title+body
    claims... unless there is a deterministic reason to preserve
    separately enumerated goals").

    The one deterministic reason to preserve more than one claim: the
    body itself explicitly enumerates goals as a markdown bullet/numbered
    list (see :func:`_extract_enumerated_goals`) -- each individually-
    sufficient bullet becomes its own claim, bounded to
    :data:`~patchfrog.intent_verification.domain.MAX_INTENT_CLAIMS`.
    Prose without that explicit structure is never split (spec section
    7: "Otherwise prefer one conservative combined claim.")."""

    title_normalized = normalize_intent_text(title) if title else ""
    body_normalized = normalize_intent_text(body) if body else ""
    body_sufficient = bool(body_normalized) and is_intent_evidence_sufficient(body_normalized)
    title_sufficient = bool(title_normalized) and is_intent_evidence_sufficient(title_normalized)

    if body_sufficient:
        assert body is not None  # body_sufficient implies body_normalized is non-empty, so body is too
        enumerated = _extract_enumerated_goals(body)
        if enumerated is not None:
            goal_claims = [
                _build_claim(source_kind=IntentSourceKind.PR_BODY, source_identifier=f"body[{i}]", text=normalized)
                for i, goal in enumerate(enumerated)
                if is_intent_evidence_sufficient(normalized := normalize_intent_text(goal))
            ]
            if len(goal_claims) >= 2:
                return tuple(goal_claims[:MAX_INTENT_CLAIMS])
        return (_build_claim(source_kind=IntentSourceKind.PR_BODY, source_identifier="body", text=body_normalized),)

    if title_sufficient:
        return (_build_claim(source_kind=IntentSourceKind.PR_TITLE, source_identifier="title", text=title_normalized),)

    return ()


def _build_claim(*, source_kind: IntentSourceKind, source_identifier: str, text: str) -> IntentClaim:
    evidence = IntentEvidence(
        source_kind=source_kind, source_identifier=source_identifier, bounded_text=text,
        strength=IntentStrength.EXPLICIT,
    )
    claim_id = hashlib.sha256(f"{source_kind.value}\x1f{text}".encode()).hexdigest()[:16]
    return IntentClaim(id=claim_id, normalized_statement=text, source=evidence, strength=IntentStrength.EXPLICIT)
