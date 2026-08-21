"""Unit coverage for :mod:`patchfrog.evaluation.metrics` -- pure
aggregation over already-matched :class:`CaseResult` rows. No I/O, no
matcher re-invocation: every test builds its outcomes directly."""

from __future__ import annotations

from patchfrog.analysis.domain import FindingCategory, Severity
from patchfrog.evaluation.domain import (
    AnalyzerExecutionSummary,
    CaseResult,
    CaseStatus,
    Difficulty,
    EvaluationCase,
    EvaluationMode,
    ExpectedFinding,
    ExpectedFindingOutcome,
    ExpectedOutcome,
    FixtureInfo,
    Language,
    MatchOutcome,
    PredictedFinding,
    PredictionOutcome,
    PredictionSource,
)
from patchfrog.evaluation.metrics import (
    compute_category_metrics,
    compute_clean_case_metrics,
    compute_critic_comparison,
    compute_difficulty_metrics,
    compute_hallucination_metrics,
    compute_overall_metrics,
    compute_severity_metrics,
    compute_static_ai_overlap,
    compute_static_analyzer_coverage,
)


def _pred(source: PredictionSource = PredictionSource.AI, severity: Severity = Severity.HIGH) -> PredictedFinding:
    return PredictedFinding(
        source=source, category=FindingCategory.CORRECTNESS, severity=severity, title="t", message="m",
        file_path="a.py", start_line=10, end_line=10, symbol_qualified_name="foo", evidence_text="x",
    )


def _expected(severity: Severity = Severity.HIGH) -> ExpectedFinding:
    return ExpectedFinding(id="ef1", category=FindingCategory.CORRECTNESS, file="a.py", issue_family="fam", severity=severity)


def _case(case_id: str, *, expected: tuple[ExpectedFinding, ...] = (), difficulty: Difficulty = Difficulty.EASY) -> EvaluationCase:
    return EvaluationCase(
        id=case_id, title="t", description="d", language=Language.PYTHON, fixture=case_id, difficulty=difficulty,
        expected=expected,
    )


def _result(
    case_id: str, *, predictions: tuple[PredictionOutcome, ...] = (), expected_outcomes: tuple[ExpectedFindingOutcome, ...] = (),
    status: CaseStatus = CaseStatus.COMPLETED_WITH_FINDINGS, proposals: tuple[PredictedFinding, ...] = (),
    candidates_reviewed: int = 1, analyzer_executions: tuple[AnalyzerExecutionSummary, ...] = (),
) -> CaseResult:
    return CaseResult(
        case_id=case_id, mode=EvaluationMode.FULL_PIPELINE, status=status, duration_ms=1.0,
        predictions=predictions, expected_outcomes=expected_outcomes, proposals_before_validation=proposals,
        candidates_reviewed=candidates_reviewed, analyzer_executions=analyzer_executions,
    )


def test_precision_recall_f1_basic() -> None:
    tp = PredictionOutcome(prediction=_pred(), outcome=MatchOutcome.TRUE_POSITIVE, matched_expected_id="ef1", detail="")
    fp = PredictionOutcome(prediction=_pred(), outcome=MatchOutcome.FALSE_POSITIVE, matched_expected_id=None, detail="")
    missed = ExpectedFindingOutcome(expected=_expected(), outcome=ExpectedOutcome.MISSED, matched_prediction_index=None, detail="")
    found = ExpectedFindingOutcome(expected=_expected(), outcome=ExpectedOutcome.FOUND, matched_prediction_index=0, detail="")

    results = [_result("c1", predictions=(tp, fp), expected_outcomes=(found, missed))]
    m = compute_overall_metrics(results)
    assert m.confusion.true_positives == 1
    assert m.confusion.false_positives == 1
    assert m.confusion.missed == 1
    assert m.scores.precision == 0.5
    assert m.scores.recall == 0.5
    assert round(m.scores.f1, 4) == 0.5


def test_precision_is_perfect_with_zero_predictions() -> None:
    # No predictions, no expected findings -- an empty confusion matrix
    # must never be treated as a division-by-zero failure.
    m = compute_overall_metrics([_result("c1", status=CaseStatus.PASSED)])
    assert m.scores.precision == 1.0
    assert m.scores.recall == 1.0


def test_error_cases_excluded_from_overall_metrics() -> None:
    tp = PredictionOutcome(prediction=_pred(), outcome=MatchOutcome.TRUE_POSITIVE, matched_expected_id="ef1", detail="")
    good = _result("c1", predictions=(tp,))
    infra_error = _result("c2", status=CaseStatus.INFRASTRUCTURE_ERROR)
    m = compute_overall_metrics([good, infra_error])
    assert m.cases == 1
    assert m.error_cases == 1
    assert m.confusion.true_positives == 1  # the infra-errored case contributes nothing


def test_clean_case_pass_rate() -> None:
    clean_case = _case("clean1", expected=())
    dirty_result = _result(
        "clean1",
        predictions=(PredictionOutcome(prediction=_pred(), outcome=MatchOutcome.FALSE_POSITIVE, matched_expected_id=None, detail=""),),
    )
    clean_result = _result("clean2", predictions=())
    cases_by_id = {"clean1": clean_case, "clean2": _case("clean2", expected=())}
    m = compute_clean_case_metrics([dirty_result, clean_result], cases_by_id)
    assert m.clean_cases == 2
    assert m.clean_cases_passed == 1
    assert m.pass_rate == 0.5
    assert m.worst_case_id == "clean1"


def test_clean_case_metrics_ignore_bug_cases() -> None:
    bug_case = _case("bug1", expected=(_expected(),))
    result = _result("bug1", predictions=())
    m = compute_clean_case_metrics([result], {"bug1": bug_case})
    assert m.clean_cases == 0
    assert m.pass_rate == 1.0  # vacuously true, never a false failure


def test_severity_exact_match_and_overstatement() -> None:
    expected_high = ExpectedFindingOutcome(expected=_expected(severity=Severity.HIGH), outcome=ExpectedOutcome.FOUND, matched_prediction_index=0, detail="")
    tp_exact = PredictionOutcome(prediction=_pred(severity=Severity.HIGH), outcome=MatchOutcome.TRUE_POSITIVE, matched_expected_id="ef1", detail="")
    m1 = compute_severity_metrics([_result("c1", predictions=(tp_exact,), expected_outcomes=(expected_high,))])
    assert m1.exact_match_rate == 1.0
    assert m1.overstatement_rate == 0.0

    tp_over = PredictionOutcome(prediction=_pred(severity=Severity.CRITICAL), outcome=MatchOutcome.TRUE_POSITIVE, matched_expected_id="ef1", detail="")
    m2 = compute_severity_metrics([_result("c1", predictions=(tp_over,), expected_outcomes=(expected_high,))])
    assert m2.exact_match_rate == 0.0
    assert m2.overstatement_rate == 1.0
    assert m2.within_one_level_rate == 1.0  # HIGH -> CRITICAL is one level up


def test_severity_understatement() -> None:
    expected_high = ExpectedFindingOutcome(expected=_expected(severity=Severity.HIGH), outcome=ExpectedOutcome.FOUND, matched_prediction_index=0, detail="")
    tp_under = PredictionOutcome(prediction=_pred(severity=Severity.LOW), outcome=MatchOutcome.TRUE_POSITIVE, matched_expected_id="ef1", detail="")
    m = compute_severity_metrics([_result("c1", predictions=(tp_under,), expected_outcomes=(expected_high,))])
    assert m.understatement_rate == 1.0
    assert m.within_one_level_rate == 0.0  # HIGH -> LOW is two levels down


def test_category_breakdown_separates_categories() -> None:
    tp = PredictionOutcome(prediction=_pred(), outcome=MatchOutcome.TRUE_POSITIVE, matched_expected_id="ef1", detail="")
    found = ExpectedFindingOutcome(expected=_expected(), outcome=ExpectedOutcome.FOUND, matched_prediction_index=0, detail="")
    metrics = compute_category_metrics([_result("c1", predictions=(tp,), expected_outcomes=(found,))])
    assert len(metrics) == 1
    assert metrics[0].category is FindingCategory.CORRECTNESS
    assert metrics[0].scores.precision == 1.0


def test_difficulty_breakdown_never_hides_low_support() -> None:
    easy_case = _case("c1", expected=(_expected(),), difficulty=Difficulty.EASY)
    hard_case = _case("c2", expected=(_expected(),), difficulty=Difficulty.HARD)
    missed = ExpectedFindingOutcome(expected=_expected(), outcome=ExpectedOutcome.MISSED, matched_prediction_index=None, detail="")
    results = [_result("c1", expected_outcomes=(missed,)), _result("c2", expected_outcomes=(missed,))]
    by_difficulty = compute_difficulty_metrics(results, {"c1": easy_case, "c2": hard_case})
    difficulties = {d.difficulty for d in by_difficulty}
    assert difficulties == {Difficulty.EASY, Difficulty.HARD}
    for d in by_difficulty:
        assert d.support == 1
        assert d.scores.recall == 0.0


def test_static_ai_overlap_classifies_both_and_either() -> None:
    static_tp = PredictionOutcome(prediction=_pred(source=PredictionSource.STATIC), outcome=MatchOutcome.TRUE_POSITIVE, matched_expected_id="ef1", detail="")
    ai_dup = PredictionOutcome(prediction=_pred(source=PredictionSource.AI), outcome=MatchOutcome.DUPLICATE, matched_expected_id="ef1", detail="")
    found = ExpectedFindingOutcome(expected=_expected(), outcome=ExpectedOutcome.FOUND, matched_prediction_index=0, detail="")
    overlap = compute_static_ai_overlap([_result("c1", predictions=(static_tp, ai_dup), expected_outcomes=(found,))])
    assert overlap is not None
    assert overlap.both == 1
    assert overlap.static_only == 0
    assert overlap.ai_only == 0


def test_static_ai_overlap_none_when_no_predictions_ever_matched() -> None:
    assert compute_static_ai_overlap([_result("c1", status=CaseStatus.PASSED)]) is None


def test_hallucination_metrics_before_and_after_validation() -> None:
    fixture_info = {"c1": FixtureInfo(valid_file_paths=frozenset({"a.py"}), file_line_counts={"a.py": 50})}
    hallucinated_proposal = PredictedFinding(
        source=PredictionSource.AI, category=FindingCategory.CORRECTNESS, severity=Severity.HIGH, title="t",
        message="m", file_path="nonexistent.py", start_line=1, end_line=1, symbol_qualified_name=None, evidence_text="x",
    )
    valid_proposal = _pred()
    result = _result("c1", proposals=(hallucinated_proposal, valid_proposal))
    m = compute_hallucination_metrics([result], fixture_info)
    assert m.proposals_before_validation == 2
    assert m.unsupported_before_validation == 1
    assert m.unsupported_rate_before_validation == 0.5


def test_critic_comparison_deltas() -> None:
    tp = PredictionOutcome(prediction=_pred(), outcome=MatchOutcome.TRUE_POSITIVE, matched_expected_id="ef1", detail="")
    fp = PredictionOutcome(prediction=_pred(), outcome=MatchOutcome.FALSE_POSITIVE, matched_expected_id=None, detail="")
    off = [_result("c1", predictions=(tp, fp))]
    on = [_result("c1", predictions=(tp,))]  # critic correctly rejected the false positive
    comparison = compute_critic_comparison(off, on)
    assert comparison.false_positive_delta == -1
    assert comparison.precision_delta > 0


def test_static_analyzer_coverage_aggregates_across_cases() -> None:
    r1 = _result(
        "c1",
        analyzer_executions=(
            AnalyzerExecutionSummary(analyzer="ruff", status="succeeded", raw_findings_count=1),
            AnalyzerExecutionSummary(analyzer="cppcheck", status="unsupported", raw_findings_count=0),
        ),
    )
    r2 = _result(
        "c2",
        analyzer_executions=(
            AnalyzerExecutionSummary(analyzer="ruff", status="succeeded", raw_findings_count=0),
            AnalyzerExecutionSummary(analyzer="cppcheck", status="unsupported", raw_findings_count=0),
        ),
    )
    coverage = compute_static_analyzer_coverage([r1, r2])
    by_name = {c.analyzer: c for c in coverage}
    assert by_name["ruff"].attempted == 2
    assert by_name["ruff"].succeeded == 2
    assert by_name["ruff"].total_raw_findings == 1
    assert by_name["cppcheck"].attempted == 2
    assert by_name["cppcheck"].succeeded == 0
    assert by_name["cppcheck"].unsupported == 2


def test_static_analyzer_coverage_excludes_error_cases() -> None:
    good = _result("c1", analyzer_executions=(AnalyzerExecutionSummary(analyzer="ruff", status="succeeded", raw_findings_count=1),))
    errored = CaseResult(
        case_id="c2", mode=EvaluationMode.STATIC_ONLY, status=CaseStatus.INFRASTRUCTURE_ERROR, duration_ms=1.0,
        analyzer_executions=(AnalyzerExecutionSummary(analyzer="ruff", status="succeeded", raw_findings_count=99),),
    )
    coverage = compute_static_analyzer_coverage([good, errored])
    assert len(coverage) == 1
    assert coverage[0].attempted == 1
    assert coverage[0].total_raw_findings == 1


def test_static_analyzer_coverage_empty_when_no_executions() -> None:
    assert compute_static_analyzer_coverage([_result("c1")]) == ()
