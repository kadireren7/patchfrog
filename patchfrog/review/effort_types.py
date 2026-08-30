"""Typed Quality + Cost Guard vocabulary.

Split out from :mod:`patchfrog.review.effort` (which depends on
:mod:`patchfrog.review.domain`) so that ``domain.py`` itself can use
these types (e.g. ``ReviewRunSummary.candidates_by_tier``) without a
circular import -- the same leaf-module pattern already used for
:class:`~patchfrog.review.agents.roles.AgentRole`.
"""

from __future__ import annotations

from enum import StrEnum


class ReviewEffortTier(StrEnum):
    """Exactly three tiers in v1 -- do not add more without also
    updating :class:`~patchfrog.review.effort.ReviewEffortPolicy`,
    ``docs/quality-cost-guard.md``, and the version bumps in
    :mod:`patchfrog.review.config`."""

    LIGHT = "light"
    STANDARD = "standard"
    DEEP = "deep"


class ReviewEffortReason(StrEnum):
    """Why a candidate landed at its tier -- always persisted-worthy
    audit information."""

    NO_SIGNAL = "no_signal"
    STATIC_FINDING_PRESENT = "static_finding_present"
    STATIC_HIGH_SEVERITY = "static_high_severity"
    SECURITY_RELEVANT = "security_relevant"
    HIGH_RISK_STATIC_CATEGORY = "high_risk_static_category"
    LARGE_CHANGED_SYMBOL = "large_changed_symbol"
    MANY_CHANGED_LINES = "many_changed_lines"
    MULTIPLE_STRUCTURAL_SIGNALS = "multiple_structural_signals"
    #: Escalation-only: the provisional tier was raised *before* any
    #: specialist provider call because adaptive context expansion
    #: actually occurred (see
    #: :meth:`~patchfrog.review.effort.ReviewEffortPolicy.finalize`) --
    #: concrete depth-2 evidence, not a guess.
    ADAPTIVE_EXPANSION_OCCURRED = "adaptive_expansion_occurred"
    #: Escalation-only: the tier was raised *after* specialist proposals
    #: were validated and cross-role-grouped, because a surviving
    #: proposal carries a deterministic high-risk signal (HIGH/CRITICAL
    #: severity, security category, or membership in an unresolved
    #: contradiction group) that was never accounted for at
    #: decision-time (see
    #: :meth:`~patchfrog.review.effort.ReviewEffortPolicy.escalate_for_high_risk_proposal`).
    #: A distinct, later escalation point from
    #: ``ADAPTIVE_EXPANSION_OCCURRED`` above -- this one exists precisely
    #: because LIGHT disables adaptive context, so a LIGHT candidate can
    #: never reach that escalation path at all.
    HIGH_RISK_PROPOSAL = "high_risk_proposal"


class CriticExpectation(StrEnum):
    """How strictly critic verification is required for this candidate's
    proposals -- never changes *what* the critic checks
    (:mod:`patchfrog.review.critic_selection` is unchanged), only how
    much of its normal cost-saving selectivity applies."""

    #: LIGHT: even more conservative than SELECTIVE -- skip the critic
    #: for anything that isn't objectively serious (HIGH/CRITICAL
    #: severity or security category); the normal policy's other
    #: cost-saving-adjacent triggers (uncorroborated, cross-role
    #: overlap) are relaxed away for a candidate this unremarkable.
    OPTIONAL = "optional"
    #: STANDARD: exactly today's :class:`~patchfrog.review.critic_selection.CriticSelectionPolicy`
    #: behavior, unchanged.
    SELECTIVE = "selective"
    #: DEEP (or escalated): critic runs for every valid, non-suppressed
    #: proposal, bypassing the selective policy's skip rule entirely.
    MANDATORY = "mandatory"
