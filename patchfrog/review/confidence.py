"""Deterministic confidence aggregation.

Combines the reviewer's self-reported confidence, the critic's verdict
(if any), and whether an independent static analyzer already flagged the
same location into one final :class:`~patchfrog.analysis.domain.Confidence`
and :class:`~patchfrog.analysis.domain.Severity`. Every rule here is a
fixed, explainable lookup -- never a second LLM call, and never a
free-form heuristic tuned by feel.
"""

from __future__ import annotations

from dataclasses import dataclass

from patchfrog.analysis.domain import Confidence, Severity
from patchfrog.review.domain import CriticDecision, CriticVerdict

_CONFIDENCE_RANK = {Confidence.LOW: 0, Confidence.MEDIUM: 1, Confidence.HIGH: 2}
_RANK_TO_CONFIDENCE = {v: k for k, v in _CONFIDENCE_RANK.items()}


@dataclass(frozen=True, slots=True)
class AggregatedResult:
    final_severity: Severity
    final_confidence: Confidence
    corroborated_by_static: bool


def aggregate(
    *,
    reviewer_confidence: Confidence,
    reviewer_severity: Severity,
    critic_verdict: CriticVerdict | None,
    corroborated_by_static: bool,
) -> AggregatedResult:
    """Deterministic aggregation rules, applied in order:

    1. A critic ``reject`` is handled by the caller before this function is
       ever invoked (the finding is suppressed, not aggregated) -- this
       function only ever sees ``accept`` or ``downgrade`` verdicts, or no
       critic at all.
    2. A ``downgrade`` verdict's ``downgraded_severity``/``downgraded_confidence``
       replace the reviewer's own values wherever supplied.
    3. Static corroboration (an independent analyzer flagged the same
       location) raises confidence by one step, capped at ``high`` --
       never lowers it, and never affects severity (a static tool's
       severity scale isn't directly comparable).
    4. Confidence is never raised above what the critic (when present)
       was willing to accept -- corroboration can only move it up to the
       critic's own ceiling, not past it.
    """

    if critic_verdict is not None and critic_verdict.decision == CriticDecision.REJECT:
        raise ValueError("aggregate() must not be called on a rejected proposal")

    severity = reviewer_severity
    confidence = reviewer_confidence
    critic_ceiling: Confidence | None = None

    if critic_verdict is not None and critic_verdict.decision == CriticDecision.DOWNGRADE:
        if critic_verdict.downgraded_severity is not None:
            severity = critic_verdict.downgraded_severity
        if critic_verdict.downgraded_confidence is not None:
            confidence = critic_verdict.downgraded_confidence
            critic_ceiling = critic_verdict.downgraded_confidence

    if corroborated_by_static:
        boosted_rank = min(_CONFIDENCE_RANK[confidence] + 1, _CONFIDENCE_RANK[Confidence.HIGH])
        boosted = _RANK_TO_CONFIDENCE[boosted_rank]
        if critic_ceiling is None or _CONFIDENCE_RANK[boosted] <= _CONFIDENCE_RANK[critic_ceiling]:
            confidence = boosted

    return AggregatedResult(
        final_severity=severity, final_confidence=confidence, corroborated_by_static=corroborated_by_static
    )


def meets_minimum(confidence: Confidence, *, minimum: Confidence) -> bool:
    return _CONFIDENCE_RANK[confidence] >= _CONFIDENCE_RANK[minimum]
