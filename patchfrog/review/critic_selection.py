"""Deterministic critic/verifier selection.

Running the critic on every valid proposal unconditionally is the
pre-orchestration behavior; with two specialist agents instead of one,
doing that unconditionally would double critic spend for no reason on
proposals that clearly don't need a second check. :class:`CriticSelectionPolicy`
is the explicit, testable seam requested by the Agent Orchestration v1
spec: a fixed, explainable rule set, never a magic heuristic tuned by
feel, and never itself a provider call.

The policy is intentionally conservative -- it only *skips* the critic
in two narrow, explainable cases (see :meth:`CriticSelectionPolicy.should_critique`).
Everything else still gets critiqued, so today's "critic every valid
proposal" behavior is preserved for anything remotely risky.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from patchfrog.analysis.domain import Confidence, FindingCategory, Severity
from patchfrog.review.agents.roles import AgentRole

_CONFIDENCE_RANK = {Confidence.LOW: 0, Confidence.MEDIUM: 1, Confidence.HIGH: 2}
_HIGH_RISK_SEVERITIES = (Severity.HIGH, Severity.CRITICAL)


@dataclass(frozen=True, slots=True)
class CriticSelectionInput:
    """The minimal, already-known-at-selection-time facts the policy
    needs -- deliberately not the full :class:`~patchfrog.review.agents.proposal.AgentProposal`,
    so the policy can never accidentally depend on something only known
    *after* critique (e.g. the critic's own verdict)."""

    role: AgentRole
    category: FindingCategory
    severity: Severity
    confidence: Confidence
    corroborated_by_static: bool
    file_path: str
    start_line: int
    end_line: int


def _overlaps(a: CriticSelectionInput, b: CriticSelectionInput) -> bool:
    if a.file_path != b.file_path:
        return False
    return not (a.end_line < b.start_line or b.end_line < a.start_line)


class CriticSelectionPolicy:
    """Rules applied in order; the first matching rule decides:

    1. **Guaranteed below publish threshold** -- even the best-case
       confidence (one static-corroboration boost, capped at ``high``)
       would still fall below ``min_final_confidence``. Critiquing
       something that can never publish wastes a call. Skip.
    2. HIGH/CRITICAL severity -- always verify. Critique.
    3. Security category -- always verify. Critique.
    4. LOW/MEDIUM reviewer confidence -- the proposal itself is already
       uncertain; a second check is warranted. Critique.
    5. Overlaps another valid proposal from a *different* role for the
       same candidate -- a conflicting/overlapping cross-role pair
       always gets verified (feeds into contradiction handling; see
       :mod:`patchfrog.review.agents.cross_role`). Critique.
    6. Not corroborated by static analysis -- an AI-only claim with none
       of the above risk signals still gets one independent check.
       Critique.
    7. Otherwise: HIGH confidence, non-security, non-HIGH/CRITICAL
       severity, static-corroborated, and no cross-role overlap -- the
       one case this policy actually skips. This is a genuinely
       low-risk, well-supported proposal; spending a critic call here
       buys little.

    Exact-duplicate-across-roles cost saving ("a duplicate already has a
    stronger verified equivalent") is deliberately NOT implemented as a
    critic-selection rule in v1 -- it would require sequencing critique
    of one proposal before deciding whether to critique the other, which
    this policy (evaluated once, before any critique happens) cannot see.
    That case is instead handled after critique, by cross-role dedup
    (:mod:`patchfrog.review.agents.cross_role`), which is an acceptable
    v1 simplification: both get critiqued, then collapsed to one.
    """

    def should_critique(
        self,
        target: CriticSelectionInput,
        *,
        peers: Sequence[CriticSelectionInput],
        min_final_confidence: Confidence,
    ) -> bool:
        best_case_rank = min(_CONFIDENCE_RANK[target.confidence] + (1 if target.corroborated_by_static else 0), 2)
        if best_case_rank < _CONFIDENCE_RANK[min_final_confidence]:
            return False

        if target.severity in _HIGH_RISK_SEVERITIES:
            return True
        if target.category is FindingCategory.SECURITY:
            return True
        if target.confidence is not Confidence.HIGH:
            return True
        if any(_overlaps(target, peer) for peer in peers):
            return True
        return not target.corroborated_by_static
