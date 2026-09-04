"""Intent Verification Foundation (Milestone L).

Extends :mod:`patchfrog.change_intelligence` and
:mod:`patchfrog.contract_intelligence` (never a third parallel
intelligence stack -- see ``docs/intent-verification.md`` and
``validation/intent_verification/latest-summary.md`` for the audit that
established this): given a review run's already-fetched PR title/body
and the already-built :class:`~patchfrog.change_intelligence.domain.ChangeUnit`/
:class:`~patchfrog.contract_intelligence.domain.ContractDelta` evidence,
this package deterministically extracts a small, bounded number of
explicit intent claims, maps them to graph-backed change evidence by
lexical relevance (never embeddings, never an LLM), and derives
evidence-backed candidates for behavior the PR explicitly claims to
implement but whose real, graph-connected surface remains unchanged.

Fails closed at every stage: vague/insufficient PR text produces zero
claims, an unmapped claim produces zero gap candidates, and every
constructed candidate is internal-only evidence for the existing
reviewer -- never an automatically published finding.

Zero LLM calls anywhere in this package (see
``test_intent_verification_never_calls_a_provider``). No new agent role
-- Correctness/Security + Critic remain the only specialists.
"""

from __future__ import annotations
