"""Deterministic intent-claim -> ChangeUnit mapping (spec section 8).

Bounded lexical overlap only -- no embeddings, no vector database, no
LLM. A claim maps to a unit only when they share at least one
meaningful token (see :mod:`patchfrog.intent_verification.lexical`);
ties are broken deterministically by unit id, and mapping is capped at
:data:`~patchfrog.intent_verification.domain.MAX_MAPPED_UNITS_PER_CLAIM`.
An unrelated unit with zero shared tokens is never mapped (spec section
8's corpus case 9).
"""

from __future__ import annotations

from patchfrog.change_intelligence.domain import ChangeUnit
from patchfrog.intent_verification.domain import MAX_MAPPED_UNITS_PER_CLAIM, IntentClaim
from patchfrog.intent_verification.lexical import meaningful_tokens


def change_unit_terms(unit: ChangeUnit) -> frozenset[str]:
    terms: set[str] = set(meaningful_tokens(unit.title))
    for candidate in unit.changed_candidates:
        if candidate.qualified_name:
            terms |= meaningful_tokens(candidate.qualified_name)
        if candidate.symbol_name:
            terms |= meaningful_tokens(candidate.symbol_name)
        terms |= meaningful_tokens(candidate.file_path)
    return frozenset(terms)


def map_claim_to_units(
    claim: IntentClaim, change_units: tuple[ChangeUnit, ...]
) -> tuple[tuple[ChangeUnit, frozenset[str]], ...]:
    """Returns the mapped units (bounded, ranked by overlap size, tie-
    broken by unit id) paired with the exact shared terms that justified
    the match -- always explainable, never a silent score."""

    claim_terms = meaningful_tokens(claim.normalized_statement)
    if not claim_terms:
        return ()

    scored: list[tuple[int, ChangeUnit, frozenset[str]]] = []
    for unit in change_units:
        overlap = claim_terms & change_unit_terms(unit)
        if overlap:
            scored.append((len(overlap), unit, overlap))

    if not scored:
        return ()

    scored.sort(key=lambda triple: (-triple[0], triple[1].id))
    return tuple((unit, overlap) for _, unit, overlap in scored[:MAX_MAPPED_UNITS_PER_CLAIM])
