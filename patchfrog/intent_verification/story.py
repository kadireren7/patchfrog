"""Deterministic Change Story prefix (spec section 21) -- one bounded
sentence, prepended to the existing Change/Contract Story text, never a
separate publication block. Only when *usable* explicit intent exists
(spec: "No intent text on vague/insufficient PR descriptions.") -- since
:func:`build_intent_story_prefix` only ever receives claims that already
passed :func:`~patchfrog.intent_verification.extraction.is_intent_evidence_sufficient`,
there is no additional gate here."""

from __future__ import annotations

from patchfrog.intent_verification.domain import IntentClaim

_MAX_STATEMENT_CHARS = 160


def build_intent_story_prefix(claims: tuple[IntentClaim, ...]) -> str:
    if not claims:
        return ""

    primary = claims[0]
    statement = primary.normalized_statement
    if len(statement) > _MAX_STATEMENT_CHARS:
        statement = statement[:_MAX_STATEMENT_CHARS].rstrip() + "..."
    return f"Intent: {statement}"
