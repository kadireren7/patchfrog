"""Contract & Blast Radius Intelligence (Milestone K).

Extends :mod:`patchfrog.change_intelligence` (never a second parallel
intelligence stack -- see ``docs/contract-intelligence.md`` and
``validation/contract_intelligence/latest-summary.md`` for the audit
that established this): given a review run's already-generated
:class:`~patchfrog.review.domain.ReviewCandidate` list plus the PR's
base commit SHA, this package deterministically detects real,
evidence-backed function-contract changes (base signature vs. head
signature), derives their bounded blast radius by reusing
:func:`patchfrog.change_intelligence.affected_surface.derive_affected_surface`
directly, and produces internal-only stale-consumer candidates using the
*existing* :class:`~patchfrog.change_intelligence.domain.ExpectedCompanionChange`
type (never a parallel candidate model).

Zero LLM calls anywhere in this package (see
``test_contract_intelligence_never_calls_a_provider``). No new agent
role -- Correctness/Security + Critic remain the only specialists.
"""

from __future__ import annotations
