"""Deterministic security-review-quality metrics.

Operates only on predictions the core matcher (:mod:`patchfrog.evaluation.matcher`)
already classified as ``TRUE_POSITIVE``, scored against the quality-metadata
ground truth on the :class:`~patchfrog.evaluation.domain.ExpectedFinding`
each one matched. This is a deliberately *separate* scoring pass from
TP/FP/missed -- it never changes whether a finding counts as a true
positive, it only asks a second question about an already-confirmed one:
was it actually *useful*?

No LLM judge, ever. Every check here is a deterministic field-presence
check, enum comparison, or curated-substring match against human-authored
ground truth. Two of these (``generic_advice_rate``,
``low_confidence_overclaim_rate``) are explicitly approximate --
"generic advice" and "overclaiming" are not perfectly automatable
questions in the general case, so both are documented as heuristic,
curated-phrase-list checks, never a claim of perfect semantic
measurement (Phase 8 spec follow-up section 21/26).

Every metric here is computed against whatever provider produced the run
(oracle or live) -- under the oracle, most of these are close to
vacuous (the oracle echoes the case's own ground truth verbatim), so a
report must always carry the same ``pipeline_correctness``/``ai_quality``
label the rest of Phase 8 already uses. See
:mod:`patchfrog.evaluation.oracle`'s module docstring.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from patchfrog.analysis.domain import Confidence
from patchfrog.evaluation.domain import (
    CaseResult,
    EvaluationCase,
    ExpectedFinding,
    MatchOutcome,
    PredictedFinding,
    severity_level,
)

#: A small, curated set of generic security/best-practice phrases that
#: are never useful on their own -- present only as a best-effort
#: heuristic signal, deliberately short to avoid brittle exact-string
#: policing (Phase 8 spec follow-up section 26). A finding matching one
#: of these AND containing nothing more specific is a weak finding; this
#: metric flags the phrase's presence, not a certainty of genericness.
GENERIC_ADVICE_PHRASES: tuple[str, ...] = (
    "validate user input",
    "validate all input",
    "consider using safer apis",
    "be careful with authentication",
    "ensure proper synchronization",
    "avoid logging sensitive data",
    "follow security best practices",
    "sanitize input",
    "use proper error handling",
)

#: Absolute/confirmed-sounding phrases that should not appear unhedged
#: on a MEDIUM/LOW-confidence finding (Phase 8 spec follow-up section 6).
_ABSOLUTE_CLAIM_PHRASES: tuple[str, ...] = (
    "this allows",
    "this is vulnerable to",
    "confirmed vulnerability",
    "definitely exploitable",
    "this enables",
)
#: Any of these present alongside an absolute claim is treated as an
#: adequate hedge -- the finding isn't overclaiming, it's already
#: conditional.
_HEDGE_PHRASES: tuple[str, ...] = ("if ", "may ", "could ", "verify", "assuming", "depends on", "unclear")


@dataclass(frozen=True, slots=True)
class SecurityQualityMetrics:
    true_positives_scored: int
    identification_present_rate: float
    root_cause_present_rate: float
    actionable_fix_present_rate: float
    actionable_fix_expected_count: int
    impact_grounded_rate: float
    impact_expected_count: int
    severity_overstatement_rate: float
    severity_checked_count: int
    unsupported_impact_rate: float
    generic_advice_rate: float
    low_confidence_overclaim_rate: float
    low_or_medium_confidence_count: int


def _contains_any(haystack: str, phrases: Sequence[str]) -> bool:
    lowered = haystack.lower()
    return any(p in lowered for p in phrases)


def compute_security_quality_metrics(
    results: Sequence[CaseResult], *, cases_by_id: Mapping[str, EvaluationCase]
) -> SecurityQualityMetrics:
    scored_pairs: list[tuple[PredictedFinding, ExpectedFinding]] = []
    for r in results:
        if r.is_error:
            continue
        case = cases_by_id.get(r.case_id)
        if case is None:
            continue
        expected_by_id = {e.id: e for e in case.expected}
        for p in r.predictions:
            if p.outcome is not MatchOutcome.TRUE_POSITIVE or p.matched_expected_id is None:
                continue
            expected = expected_by_id.get(p.matched_expected_id)
            if expected is None:
                continue
            scored_pairs.append((p.prediction, expected))

    total = len(scored_pairs)
    if total == 0:
        return SecurityQualityMetrics(
            true_positives_scored=0, identification_present_rate=1.0, root_cause_present_rate=1.0,
            actionable_fix_present_rate=1.0, actionable_fix_expected_count=0, impact_grounded_rate=1.0,
            impact_expected_count=0, severity_overstatement_rate=0.0, severity_checked_count=0,
            unsupported_impact_rate=0.0, generic_advice_rate=0.0, low_confidence_overclaim_rate=0.0,
            low_or_medium_confidence_count=0,
        )

    identification_present = sum(1 for pred, _ in scored_pairs if pred.message.strip())
    root_cause_present = sum(1 for pred, _ in scored_pairs if pred.reasoning_summary.strip())

    fix_expected = [(pred, exp) for pred, exp in scored_pairs if exp.acceptable_remediation_direction is not None]
    fix_present = sum(1 for pred, _ in fix_expected if pred.suggested_fix and pred.suggested_fix.strip())

    impact_expected = [(pred, exp) for pred, exp in scored_pairs if exp.expected_impact_concept is not None]
    impact_grounded = sum(
        1 for pred, exp in impact_expected
        if pred.impact and pred.impact.strip() and not _contains_any(pred.impact, exp.forbidden_exaggerated_claims)
    )

    severity_checked = [(pred, exp) for pred, exp in scored_pairs if exp.max_justified_severity is not None]
    overstated = sum(
        1 for pred, exp in severity_checked
        if exp.max_justified_severity is not None and severity_level(pred.severity) > severity_level(exp.max_justified_severity)
    )

    with_impact = [(pred, exp) for pred, exp in scored_pairs if pred.impact and pred.impact.strip()]
    unsupported_impact = sum(
        1 for pred, exp in with_impact if pred.impact and _contains_any(pred.impact, exp.forbidden_exaggerated_claims)
    )

    generic_advice = sum(
        1 for pred, _ in scored_pairs
        if _contains_any(pred.message, GENERIC_ADVICE_PHRASES)
        or _contains_any(pred.reasoning_summary, GENERIC_ADVICE_PHRASES)
        or (pred.suggested_fix is not None and _contains_any(pred.suggested_fix, GENERIC_ADVICE_PHRASES))
    )

    low_or_medium = [
        (pred, exp) for pred, exp in scored_pairs if pred.confidence in (Confidence.LOW, Confidence.MEDIUM)
    ]
    overclaimed = 0
    for pred, _exp in low_or_medium:
        combined = f"{pred.message} {pred.reasoning_summary}"
        if _contains_any(combined, _ABSOLUTE_CLAIM_PHRASES) and not _contains_any(combined, _HEDGE_PHRASES):
            overclaimed += 1

    return SecurityQualityMetrics(
        true_positives_scored=total,
        identification_present_rate=identification_present / total,
        root_cause_present_rate=root_cause_present / total,
        actionable_fix_present_rate=(fix_present / len(fix_expected)) if fix_expected else 1.0,
        actionable_fix_expected_count=len(fix_expected),
        impact_grounded_rate=(impact_grounded / len(impact_expected)) if impact_expected else 1.0,
        impact_expected_count=len(impact_expected),
        severity_overstatement_rate=(overstated / len(severity_checked)) if severity_checked else 0.0,
        severity_checked_count=len(severity_checked),
        unsupported_impact_rate=(unsupported_impact / len(with_impact)) if with_impact else 0.0,
        generic_advice_rate=generic_advice / total,
        low_confidence_overclaim_rate=(overclaimed / len(low_or_medium)) if low_or_medium else 0.0,
        low_or_medium_confidence_count=len(low_or_medium),
    )
