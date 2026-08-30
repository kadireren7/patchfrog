"""Pure metric computation over a completed evaluation run's
:class:`~patchfrog.evaluation.domain.CaseResult` list.

No I/O, no database, no LLM -- every function here takes plain values
(already-matched case results, plus the fixture info
:mod:`patchfrog.evaluation.runner` built while running each case) and
returns plain dataclasses. Infrastructure-failed cases
(:attr:`~patchfrog.evaluation.domain.CaseResult.is_error`) are always
excluded from quality metrics -- never counted as false negatives (see
the module docstring of :mod:`patchfrog.evaluation.domain`'s ``CaseStatus``).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from patchfrog.analysis.domain import FindingCategory
from patchfrog.evaluation.domain import (
    AnalyzerExecutionSummary,
    CaseResult,
    Difficulty,
    EvaluationCase,
    ExpectedOutcome,
    FixtureInfo,
    MatchOutcome,
    PredictionSource,
    severity_level,
)
from patchfrog.evaluation.matcher import unsupported_reason
from patchfrog.review.effort_types import ReviewEffortTier


@dataclass(frozen=True, slots=True)
class ConfusionCounts:
    true_positives: int = 0
    false_positives: int = 0
    missed: int = 0
    duplicates: int = 0
    unsupported: int = 0
    out_of_scope: int = 0


@dataclass(frozen=True, slots=True)
class PrecisionRecallF1:
    precision: float
    recall: float
    f1: float


def _scores(tp: int, fp: int, missed: int) -> PrecisionRecallF1:
    precision = tp / (tp + fp) if (tp + fp) else 1.0
    recall = tp / (tp + missed) if (tp + missed) else 1.0
    f1 = (2 * precision * recall / (precision + recall)) if (precision + recall) else 0.0
    return PrecisionRecallF1(precision=precision, recall=recall, f1=f1)


@dataclass(frozen=True, slots=True)
class OverallMetrics:
    cases: int
    error_cases: int
    confusion: ConfusionCounts
    scores: PrecisionRecallF1
    false_positives_per_case: float
    false_positives_per_100_candidates: float
    duplicate_rate: float
    unsupported_rate: float


@dataclass(frozen=True, slots=True)
class CleanCaseMetrics:
    clean_cases: int
    clean_cases_passed: int
    pass_rate: float
    average_findings_on_clean_cases: float
    worst_case_id: str | None
    worst_case_finding_count: int


@dataclass(frozen=True, slots=True)
class SeverityMetrics:
    matched_pairs: int
    exact_match_rate: float
    within_one_level_rate: float
    overstatement_rate: float
    understatement_rate: float


@dataclass(frozen=True, slots=True)
class CategoryMetrics:
    category: FindingCategory
    support: int
    confusion: ConfusionCounts
    scores: PrecisionRecallF1


@dataclass(frozen=True, slots=True)
class DifficultyMetrics:
    difficulty: Difficulty
    support: int
    scores: PrecisionRecallF1


@dataclass(frozen=True, slots=True)
class StaticAiOverlap:
    static_only: int
    ai_only: int
    both: int
    missed_by_both: int


@dataclass(frozen=True, slots=True)
class CandidateEfficiencyMetrics:
    candidates_generated: int
    candidates_reviewed: int
    candidates_skipped: int
    provider_calls: int
    reviewer_input_tokens: int
    reviewer_output_tokens: int
    input_tokens_per_tp: float
    output_tokens_per_tp: float
    provider_calls_per_tp: float
    #: Quality + Cost Guard (patchfrog.review.effort, Milestone F)
    #: suite-level aggregates -- lets an evaluation report compare
    #: "current/uniform effort" against "quality-cost guard" runs (spec
    #: sections 23/24) without duplicating any production tiering logic.
    #: Empty/zero for a suite with no AI-review cases at all.
    candidates_by_tier: dict[ReviewEffortTier, int]
    candidates_escalated: int
    critic_calls: int
    reviewer_thinking_tokens: int
    critic_thinking_tokens: int
    retries_consumed: int


@dataclass(frozen=True, slots=True)
class HallucinationMetrics:
    proposals_before_validation: int
    unsupported_before_validation: int
    unsupported_rate_before_validation: float
    accepted_findings: int
    unsupported_after_validation: int
    unsupported_rate_after_validation: float


@dataclass(frozen=True, slots=True)
class AnalyzerCoverage:
    """Aggregated per-analyzer execution outcome across every case in a
    run -- the "is this capability actually present" answer Phase 8
    spec section 45 requires: an analyzer with zero ``succeeded``
    executions and only ``unsupported`` ones is a missing capability,
    never silently reported as "zero findings means clean code"."""

    analyzer: str
    attempted: int
    succeeded: int
    failed: int
    skipped: int
    unsupported: int
    timed_out: int
    total_raw_findings: int


@dataclass(frozen=True, slots=True)
class EvaluationMetrics:
    overall: OverallMetrics
    clean: CleanCaseMetrics
    severity: SeverityMetrics
    by_category: tuple[CategoryMetrics, ...]
    by_difficulty: tuple[DifficultyMetrics, ...]
    static_ai_overlap: StaticAiOverlap | None
    efficiency: CandidateEfficiencyMetrics
    hallucination: HallucinationMetrics
    worst_false_positive_cases: tuple[tuple[str, int], ...] = field(default_factory=tuple)
    missed_expected: tuple[tuple[str, str], ...] = field(default_factory=tuple)  # (case_id, expected_id)
    static_analyzer_coverage: tuple[AnalyzerCoverage, ...] = field(default_factory=tuple)


def _confusion_from_results(results: Sequence[CaseResult]) -> ConfusionCounts:
    tp = fp = missed = dup = unsupported = oos = 0
    for r in results:
        if r.is_error:
            continue
        for p in r.predictions:
            if p.outcome is MatchOutcome.TRUE_POSITIVE:
                tp += 1
            elif p.outcome is MatchOutcome.FALSE_POSITIVE:
                fp += 1
            elif p.outcome is MatchOutcome.DUPLICATE:
                dup += 1
            elif p.outcome is MatchOutcome.UNSUPPORTED:
                unsupported += 1
            elif p.outcome is MatchOutcome.OUT_OF_SCOPE:
                oos += 1
        for e in r.expected_outcomes:
            if e.outcome is ExpectedOutcome.MISSED:
                missed += 1
    return ConfusionCounts(
        true_positives=tp, false_positives=fp, missed=missed, duplicates=dup,
        unsupported=unsupported, out_of_scope=oos,
    )


def compute_overall_metrics(results: Sequence[CaseResult]) -> OverallMetrics:
    valid = [r for r in results if not r.is_error]
    confusion = _confusion_from_results(valid)
    scores = _scores(confusion.true_positives, confusion.false_positives, confusion.missed)
    total_predictions = sum(len(r.predictions) for r in valid)
    total_candidates = sum(r.candidates_reviewed for r in valid)
    return OverallMetrics(
        cases=len(valid),
        error_cases=len(results) - len(valid),
        confusion=confusion,
        scores=scores,
        false_positives_per_case=(confusion.false_positives / len(valid)) if valid else 0.0,
        false_positives_per_100_candidates=(
            (confusion.false_positives / total_candidates) * 100 if total_candidates else 0.0
        ),
        duplicate_rate=(confusion.duplicates / total_predictions) if total_predictions else 0.0,
        unsupported_rate=(confusion.unsupported / total_predictions) if total_predictions else 0.0,
    )


def compute_clean_case_metrics(
    results: Sequence[CaseResult], cases_by_id: Mapping[str, EvaluationCase]
) -> CleanCaseMetrics:
    clean_results = [r for r in results if not r.is_error and cases_by_id[r.case_id].is_clean]
    passed = 0
    total_findings = 0
    worst_id: str | None = None
    worst_count = -1
    for r in clean_results:
        accepted = sum(1 for p in r.predictions if p.outcome in (MatchOutcome.FALSE_POSITIVE, MatchOutcome.UNSUPPORTED))
        total_findings += accepted
        if accepted == 0:
            passed += 1
        if accepted > worst_count:
            worst_count = accepted
            worst_id = r.case_id
    return CleanCaseMetrics(
        clean_cases=len(clean_results),
        clean_cases_passed=passed,
        pass_rate=(passed / len(clean_results)) if clean_results else 1.0,
        average_findings_on_clean_cases=(total_findings / len(clean_results)) if clean_results else 0.0,
        worst_case_id=worst_id if worst_count > 0 else None,
        worst_case_finding_count=max(worst_count, 0),
    )


def compute_severity_metrics(results: Sequence[CaseResult]) -> SeverityMetrics:
    exact = within_one = over = under = 0
    total = 0
    for r in results:
        if r.is_error:
            continue
        for p in r.predictions:
            if p.outcome is not MatchOutcome.TRUE_POSITIVE or p.matched_expected_id is None:
                continue
            expected = next(
                (e.expected for e in r.expected_outcomes if e.expected.id == p.matched_expected_id), None
            )
            if expected is None or expected.severity is None:
                continue
            total += 1
            actual_level = severity_level(p.prediction.severity)
            expected_level = severity_level(expected.severity)
            diff = actual_level - expected_level
            if diff == 0:
                exact += 1
            if abs(diff) <= 1:
                within_one += 1
            if diff > 0:
                over += 1
            elif diff < 0:
                under += 1
    return SeverityMetrics(
        matched_pairs=total,
        exact_match_rate=(exact / total) if total else 1.0,
        within_one_level_rate=(within_one / total) if total else 1.0,
        overstatement_rate=(over / total) if total else 0.0,
        understatement_rate=(under / total) if total else 0.0,
    )


def compute_category_metrics(results: Sequence[CaseResult]) -> tuple[CategoryMetrics, ...]:
    by_category: dict[FindingCategory, list[CaseResult]] = {}
    for r in results:
        if r.is_error:
            continue
        categories: set[FindingCategory] = {p.prediction.category for p in r.predictions}
        categories |= {e.expected.category for e in r.expected_outcomes}
        for cat in categories:
            by_category.setdefault(cat, []).append(r)

    out: list[CategoryMetrics] = []
    for category in sorted(by_category, key=lambda c: c.value):
        subset_predictions = [
            p for r in by_category[category] for p in r.predictions if p.prediction.category is category
        ]
        subset_expected = [
            e for r in by_category[category] for e in r.expected_outcomes if e.expected.category is category
        ]
        tp = sum(1 for p in subset_predictions if p.outcome is MatchOutcome.TRUE_POSITIVE)
        fp = sum(1 for p in subset_predictions if p.outcome is MatchOutcome.FALSE_POSITIVE)
        dup = sum(1 for p in subset_predictions if p.outcome is MatchOutcome.DUPLICATE)
        unsupported = sum(1 for p in subset_predictions if p.outcome is MatchOutcome.UNSUPPORTED)
        oos = sum(1 for p in subset_predictions if p.outcome is MatchOutcome.OUT_OF_SCOPE)
        missed = sum(1 for e in subset_expected if e.outcome is ExpectedOutcome.MISSED)
        confusion = ConfusionCounts(
            true_positives=tp, false_positives=fp, missed=missed, duplicates=dup,
            unsupported=unsupported, out_of_scope=oos,
        )
        out.append(
            CategoryMetrics(
                category=category, support=len(subset_expected), confusion=confusion,
                scores=_scores(tp, fp, missed),
            )
        )
    return tuple(out)


def compute_difficulty_metrics(
    results: Sequence[CaseResult], cases_by_id: Mapping[str, EvaluationCase]
) -> tuple[DifficultyMetrics, ...]:
    by_difficulty: dict[Difficulty, list[CaseResult]] = {}
    for r in results:
        if r.is_error:
            continue
        by_difficulty.setdefault(cases_by_id[r.case_id].difficulty, []).append(r)

    out: list[DifficultyMetrics] = []
    for difficulty in Difficulty:
        subset = by_difficulty.get(difficulty, [])
        if not subset:
            continue
        confusion = _confusion_from_results(subset)
        support = sum(len(r.expected_outcomes) for r in subset)
        out.append(
            DifficultyMetrics(
                difficulty=difficulty, support=support,
                scores=_scores(confusion.true_positives, confusion.false_positives, confusion.missed),
            )
        )
    return tuple(out)


def compute_static_ai_overlap(results: Sequence[CaseResult]) -> StaticAiOverlap | None:
    """Only meaningful for a run that produced both static and AI
    predictions in the same pass (``FULL_PIPELINE``/``INCREMENTAL``) --
    derived from a single run's matched pairs (both the TRUE_POSITIVE
    and any DUPLICATE predictions sharing its ``matched_expected_id``),
    never from two separately-run modes that could drift out of sync."""

    static_only = ai_only = both = missed_by_both = 0
    saw_any = False
    for r in results:
        if r.is_error:
            continue
        sources_by_expected: dict[str, set[PredictionSource]] = {}
        for p in r.predictions:
            if p.matched_expected_id is None:
                continue
            sources_by_expected.setdefault(p.matched_expected_id, set()).add(p.prediction.source)
            saw_any = True
        for e in r.expected_outcomes:
            sources = sources_by_expected.get(e.expected.id, set())
            if e.outcome is ExpectedOutcome.MISSED:
                missed_by_both += 1
            elif sources == {PredictionSource.STATIC}:
                static_only += 1
            elif sources == {PredictionSource.AI}:
                ai_only += 1
            elif sources:
                both += 1

    if not saw_any and static_only == ai_only == both == 0:
        return None
    return StaticAiOverlap(static_only=static_only, ai_only=ai_only, both=both, missed_by_both=missed_by_both)


def compute_efficiency_metrics(results: Sequence[CaseResult]) -> CandidateEfficiencyMetrics:
    valid = [r for r in results if not r.is_error]
    generated = sum(r.candidates_generated for r in valid)
    reviewed = sum(r.candidates_reviewed for r in valid)
    skipped = sum(r.candidates_skipped for r in valid)
    calls = sum(r.provider_calls for r in valid)
    input_tokens = sum(r.reviewer_input_tokens for r in valid)
    output_tokens = sum(r.reviewer_output_tokens for r in valid)
    tp = sum(1 for r in valid for p in r.predictions if p.outcome is MatchOutcome.TRUE_POSITIVE)

    candidates_by_tier: dict[ReviewEffortTier, int] = {}
    for r in valid:
        for tier, count in r.candidates_by_tier.items():
            candidates_by_tier[tier] = candidates_by_tier.get(tier, 0) + count

    return CandidateEfficiencyMetrics(
        candidates_generated=generated, candidates_reviewed=reviewed, candidates_skipped=skipped,
        provider_calls=calls, reviewer_input_tokens=input_tokens, reviewer_output_tokens=output_tokens,
        input_tokens_per_tp=(input_tokens / tp) if tp else 0.0,
        output_tokens_per_tp=(output_tokens / tp) if tp else 0.0,
        provider_calls_per_tp=(calls / tp) if tp else 0.0,
        candidates_by_tier=candidates_by_tier,
        candidates_escalated=sum(r.candidates_escalated for r in valid),
        critic_calls=sum(r.critic_calls for r in valid),
        reviewer_thinking_tokens=sum(r.reviewer_thinking_tokens for r in valid),
        critic_thinking_tokens=sum(r.critic_thinking_tokens for r in valid),
        retries_consumed=sum(r.retries_consumed for r in valid),
    )


def compute_hallucination_metrics(
    results: Sequence[CaseResult], fixture_info: Mapping[str, FixtureInfo]
) -> HallucinationMetrics:
    proposals = 0
    unsupported_before = 0
    accepted = 0
    unsupported_after = 0
    for r in results:
        if r.is_error:
            continue
        info = fixture_info.get(r.case_id)
        proposals += len(r.proposals_before_validation)
        if info is not None:
            for proposal in r.proposals_before_validation:
                if unsupported_reason(proposal, info.valid_file_paths, info.file_line_counts) is not None:
                    unsupported_before += 1
        accepted += len(r.predictions)
        unsupported_after += sum(1 for p in r.predictions if p.outcome is MatchOutcome.UNSUPPORTED)
    return HallucinationMetrics(
        proposals_before_validation=proposals,
        unsupported_before_validation=unsupported_before,
        unsupported_rate_before_validation=(unsupported_before / proposals) if proposals else 0.0,
        accepted_findings=accepted,
        unsupported_after_validation=unsupported_after,
        unsupported_rate_after_validation=(unsupported_after / accepted) if accepted else 0.0,
    )


def compute_metrics(
    results: Sequence[CaseResult],
    *,
    cases_by_id: Mapping[str, EvaluationCase],
    fixture_info: Mapping[str, FixtureInfo],
) -> EvaluationMetrics:
    worst_fp = sorted(
        (
            (r.case_id, sum(1 for p in r.predictions if p.outcome is MatchOutcome.FALSE_POSITIVE))
            for r in results if not r.is_error
        ),
        key=lambda pair: pair[1], reverse=True,
    )
    missed = tuple(
        (r.case_id, e.expected.id)
        for r in results if not r.is_error
        for e in r.expected_outcomes if e.outcome is ExpectedOutcome.MISSED
    )
    return EvaluationMetrics(
        overall=compute_overall_metrics(results),
        clean=compute_clean_case_metrics(results, cases_by_id),
        severity=compute_severity_metrics(results),
        by_category=compute_category_metrics(results),
        by_difficulty=compute_difficulty_metrics(results, cases_by_id),
        static_ai_overlap=compute_static_ai_overlap(results),
        efficiency=compute_efficiency_metrics(results),
        hallucination=compute_hallucination_metrics(results, fixture_info),
        worst_false_positive_cases=tuple(pair for pair in worst_fp[:10] if pair[1] > 0),
        missed_expected=missed,
        static_analyzer_coverage=compute_static_analyzer_coverage(results),
    )


def compute_static_analyzer_coverage(results: Sequence[CaseResult]) -> tuple[AnalyzerCoverage, ...]:
    by_analyzer: dict[str, list[AnalyzerExecutionSummary]] = {}
    for r in results:
        if r.is_error:
            continue
        for e in r.analyzer_executions:
            by_analyzer.setdefault(e.analyzer, []).append(e)

    out: list[AnalyzerCoverage] = []
    for name in sorted(by_analyzer):
        executions = by_analyzer[name]
        out.append(
            AnalyzerCoverage(
                analyzer=name,
                attempted=len(executions),
                succeeded=sum(1 for e in executions if e.status == "succeeded"),
                failed=sum(1 for e in executions if e.status == "failed"),
                skipped=sum(1 for e in executions if e.status == "skipped"),
                unsupported=sum(1 for e in executions if e.status == "unsupported"),
                timed_out=sum(1 for e in executions if e.status == "timed_out"),
                total_raw_findings=sum(e.raw_findings_count for e in executions),
            )
        )
    return tuple(out)


@dataclass(frozen=True, slots=True)
class CriticComparison:
    critic_off: OverallMetrics
    critic_on: OverallMetrics
    precision_delta: float
    recall_delta: float
    false_positive_delta: int
    unsupported_delta: int


def compute_critic_comparison(
    results_critic_off: Sequence[CaseResult], results_critic_on: Sequence[CaseResult]
) -> CriticComparison:
    off = compute_overall_metrics(results_critic_off)
    on = compute_overall_metrics(results_critic_on)
    return CriticComparison(
        critic_off=off, critic_on=on,
        precision_delta=on.scores.precision - off.scores.precision,
        recall_delta=on.scores.recall - off.scores.recall,
        false_positive_delta=on.confusion.false_positives - off.confusion.false_positives,
        unsupported_delta=on.confusion.unsupported - off.confusion.unsupported,
    )
