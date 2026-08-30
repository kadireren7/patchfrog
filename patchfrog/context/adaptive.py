"""Deterministic adaptive-expansion decision logic.

:class:`AdaptiveExpansionPolicy` decides, for one target, whether depth-1
context plausibly needs a second hop -- using only repository-structural
signals already available before any provider call (call-graph shape,
diff/changed-line data, static-finding category). It never inspects
source comments/prose, never asks an LLM, and never itself performs graph
traversal -- the depth-2 candidates it decides whether to *keep* are
computed once, bounded, by the existing
:meth:`patchfrog.context.candidates.ContextCandidateGenerator.generate`
machinery (see that module's docstring) -- this module only decides
which of them survive.

Every reason here maps to one of the deterministic signals in the
Adaptive Context milestone spec (section 3, A/B/C/E). Signal D
(constants/config/helper dependency resolution) is deliberately not
implemented -- current repository intelligence has no relationship kind
for "depends on this module-level constant," and inventing one would
mean guessing at semantics rather than reading a real edge. See
``docs/context-engine.md`` for that limitation.
"""

from __future__ import annotations

from patchfrog.analysis.domain import FindingCategory
from patchfrog.context.domain import (
    ContextCandidate,
    ContextRelationship,
    ExpansionDecision,
    ExpansionDirection,
    ExpansionReason,
)
from patchfrog.persistence.models.code_index import SymbolModel

#: C. Categories where a direct call relationship is already known to
#: matter -- mirrors :mod:`patchfrog.context.scoring`'s existing
#: category-preference table rather than inventing a second one.
_CATEGORY_TRIGGERS = frozenset(
    {
        FindingCategory.MEMORY_SAFETY,
        FindingCategory.RESOURCE_MANAGEMENT,
        FindingCategory.CONCURRENCY,
        FindingCategory.API_MISUSE,
        FindingCategory.SECURITY,
    }
)

#: E. A target this small, delegating to exactly one direct callee, is
#: structurally a thin wrapper -- the real logic is one hop further out.
_THIN_WRAPPER_MAX_SPAN_LINES = 8

#: Stable output order for :attr:`ExpansionDecision.reasons` -- built
#: from a set internally, so iteration order is never incidental.
_REASON_ORDER = (
    ExpansionReason.CALL_CHAIN_CONTINUATION,
    ExpansionReason.CHANGED_NEIGHBOR,
    ExpansionReason.STATIC_CATEGORY_RELEVANCE,
    ExpansionReason.THIN_WRAPPER,
)


class AdaptiveExpansionPolicy:
    """Pure, deterministic, side-effect-free. See module docstring."""

    def decide(
        self,
        *,
        target_symbol: SymbolModel | None,
        depth1_candidates: list[ContextCandidate],
        depth2_candidates: list[ContextCandidate],
        finding_category: FindingCategory | None,
    ) -> ExpansionDecision:
        depth1_callers = [c for c in depth1_candidates if c.relationship is ContextRelationship.DIRECT_CALLER]
        depth1_callees = [c for c in depth1_candidates if c.relationship is ContextRelationship.DIRECT_CALLEE]
        depth2_callers = [c for c in depth2_candidates if c.relationship is ContextRelationship.TRANSITIVE_CALLER]
        depth2_callees = [c for c in depth2_candidates if c.relationship is ContextRelationship.TRANSITIVE_CALLEE]

        reasons: set[ExpansionReason] = set()
        wants_callers = False
        wants_callees = False

        # A: call-chain continuation -- a resolvable second hop exists.
        if depth2_callers:
            reasons.add(ExpansionReason.CALL_CHAIN_CONTINUATION)
            wants_callers = True
        if depth2_callees:
            reasons.add(ExpansionReason.CALL_CHAIN_CONTINUATION)
            wants_callees = True

        # B: a depth-1 neighbor is itself changed in this diff, and a
        # second hop past it actually exists.
        if depth2_callers and any(c.is_on_changed_line for c in depth1_callers):
            reasons.add(ExpansionReason.CHANGED_NEIGHBOR)
            wants_callers = True
        if depth2_callees and any(c.is_on_changed_line for c in depth1_callees):
            reasons.add(ExpansionReason.CHANGED_NEIGHBOR)
            wants_callees = True

        # C: static/security category where direct calls are already
        # known to matter, and this direction has both a depth-1
        # relationship and a resolvable depth-2 continuation.
        if finding_category in _CATEGORY_TRIGGERS:
            if depth1_callers and depth2_callers:
                reasons.add(ExpansionReason.STATIC_CATEGORY_RELEVANCE)
                wants_callers = True
            if depth1_callees and depth2_callees:
                reasons.add(ExpansionReason.STATIC_CATEGORY_RELEVANCE)
                wants_callees = True

        # E: target is a thin wrapper/delegator.
        if (
            target_symbol is not None
            and (target_symbol.end_line - target_symbol.start_line) <= _THIN_WRAPPER_MAX_SPAN_LINES
            and len(depth1_callees) == 1
            and depth2_callees
        ):
            reasons.add(ExpansionReason.THIN_WRAPPER)
            wants_callees = True

        if not wants_callers and not wants_callees:
            return ExpansionDecision(expand=False)

        direction: ExpansionDirection
        if wants_callers and wants_callees:
            direction = "both"
        elif wants_callers:
            direction = "callers"
        else:
            direction = "callees"

        ordered_reasons = tuple(r for r in _REASON_ORDER if r in reasons)
        return ExpansionDecision(expand=True, reasons=ordered_reasons, direction=direction)
