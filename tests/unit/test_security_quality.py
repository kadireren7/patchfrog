"""Unit coverage for :mod:`patchfrog.evaluation.security_quality` -- pure
scoring over already-matched (TRUE_POSITIVE) :class:`CaseResult` rows.
Never re-invokes the matcher: every test builds its outcomes directly,
mirroring the style of ``test_evaluation_metrics.py``."""

from __future__ import annotations

from patchfrog.analysis.domain import Confidence, FindingCategory, Severity
from patchfrog.evaluation.domain import (
    CaseResult,
    CaseStatus,
    Difficulty,
    EvaluationCase,
    EvaluationMode,
    ExpectedFinding,
    Language,
    MatchOutcome,
    PredictedFinding,
    PredictionOutcome,
    PredictionSource,
)
from patchfrog.evaluation.security_quality import compute_security_quality_metrics


def _pred(
    *,
    message: str = "`password` is interpolated into the returned error string.",
    reasoning_summary: str = "the value reaches the response text without redaction",
    impact: str | None = "an attacker who triggers this path receives the plaintext password",
    suggested_fix: str | None = "remove `password` from the returned/logged error",
    severity: Severity = Severity.HIGH,
    confidence: Confidence = Confidence.HIGH,
) -> PredictedFinding:
    return PredictedFinding(
        source=PredictionSource.AI, category=FindingCategory.SECURITY, severity=severity, title="t",
        message=message, file_path="a.py", start_line=10, end_line=10, symbol_qualified_name="foo",
        evidence_text="x", confidence=confidence, reasoning_summary=reasoning_summary, impact=impact,
        suggested_fix=suggested_fix,
    )


def _expected(
    *,
    expected_impact_concept: str | None = "plaintext credential disclosure",
    acceptable_remediation_direction: str | None = "remove the secret from the returned/logged text",
    max_justified_severity: Severity | None = Severity.HIGH,
    forbidden_exaggerated_claims: tuple[str, ...] = (),
) -> ExpectedFinding:
    return ExpectedFinding(
        id="ef1", category=FindingCategory.SECURITY, file="a.py", issue_family="cred-exposure",
        expected_impact_concept=expected_impact_concept,
        acceptable_remediation_direction=acceptable_remediation_direction,
        max_justified_severity=max_justified_severity,
        forbidden_exaggerated_claims=forbidden_exaggerated_claims,
    )


def _case(case_id: str, expected: ExpectedFinding) -> EvaluationCase:
    return EvaluationCase(
        id=case_id, title="t", description="d", language=Language.PYTHON, fixture=case_id,
        difficulty=Difficulty.EASY, expected=(expected,),
    )


def _tp_result(case_id: str, prediction: PredictedFinding, *, expected_id: str = "ef1") -> CaseResult:
    outcome = PredictionOutcome(
        prediction=prediction, outcome=MatchOutcome.TRUE_POSITIVE, matched_expected_id=expected_id, detail="",
    )
    return CaseResult(
        case_id=case_id, mode=EvaluationMode.FULL_PIPELINE, status=CaseStatus.COMPLETED_WITH_FINDINGS,
        duration_ms=1.0, predictions=(outcome,),
    )


def test_no_scored_true_positives_returns_vacuous_all_clean_metrics() -> None:
    m = compute_security_quality_metrics([], cases_by_id={})
    assert m.true_positives_scored == 0
    assert m.identification_present_rate == 1.0
    assert m.root_cause_present_rate == 1.0
    assert m.generic_advice_rate == 0.0
    assert m.severity_overstatement_rate == 0.0


def test_only_true_positive_matches_are_scored_never_false_positives_or_missed() -> None:
    fp = PredictionOutcome(prediction=_pred(), outcome=MatchOutcome.FALSE_POSITIVE, matched_expected_id=None, detail="")
    result = CaseResult(
        case_id="c1", mode=EvaluationMode.FULL_PIPELINE, status=CaseStatus.COMPLETED_WITH_FINDINGS,
        duration_ms=1.0, predictions=(fp,),
    )
    m = compute_security_quality_metrics([result], cases_by_id={"c1": _case("c1", _expected())})
    assert m.true_positives_scored == 0


def test_well_formed_finding_scores_perfectly_across_every_rate() -> None:
    case = _case("c1", _expected())
    result = _tp_result("c1", _pred())
    m = compute_security_quality_metrics([result], cases_by_id={"c1": case})
    assert m.true_positives_scored == 1
    assert m.identification_present_rate == 1.0
    assert m.root_cause_present_rate == 1.0
    assert m.actionable_fix_present_rate == 1.0
    assert m.impact_grounded_rate == 1.0
    assert m.severity_overstatement_rate == 0.0
    assert m.unsupported_impact_rate == 0.0
    assert m.generic_advice_rate == 0.0


def test_empty_message_or_reasoning_lowers_identification_and_root_cause_rates() -> None:
    case = _case("c1", _expected())
    result = _tp_result("c1", _pred(message="", reasoning_summary=""))
    m = compute_security_quality_metrics([result], cases_by_id={"c1": case})
    assert m.identification_present_rate == 0.0
    assert m.root_cause_present_rate == 0.0


def test_missing_fix_only_counted_against_cases_that_expect_one() -> None:
    case = _case("c1", _expected(acceptable_remediation_direction=None))
    result = _tp_result("c1", _pred(suggested_fix=None))
    m = compute_security_quality_metrics([result], cases_by_id={"c1": case})
    # No remediation was expected at all -- absence of a fix must not
    # penalize the rate (denominator is 0, defaults to 1.0).
    assert m.actionable_fix_expected_count == 0
    assert m.actionable_fix_present_rate == 1.0


def test_missing_fix_when_one_is_expected_lowers_the_rate() -> None:
    case = _case("c1", _expected(acceptable_remediation_direction="use parameterized queries"))
    result = _tp_result("c1", _pred(suggested_fix=None))
    m = compute_security_quality_metrics([result], cases_by_id={"c1": case})
    assert m.actionable_fix_expected_count == 1
    assert m.actionable_fix_present_rate == 0.0


def test_severity_exceeding_max_justified_counts_as_overstated() -> None:
    case = _case("c1", _expected(max_justified_severity=Severity.MEDIUM))
    result = _tp_result("c1", _pred(severity=Severity.CRITICAL))
    m = compute_security_quality_metrics([result], cases_by_id={"c1": case})
    assert m.severity_checked_count == 1
    assert m.severity_overstatement_rate == 1.0


def test_severity_at_or_below_max_justified_is_not_overstated() -> None:
    case = _case("c1", _expected(max_justified_severity=Severity.HIGH))
    result = _tp_result("c1", _pred(severity=Severity.HIGH))
    m = compute_security_quality_metrics([result], cases_by_id={"c1": case})
    assert m.severity_overstatement_rate == 0.0


def test_forbidden_exaggerated_claim_in_impact_counts_as_unsupported_and_not_grounded() -> None:
    case = _case(
        "c1",
        _expected(
            expected_impact_concept="local information disclosure",
            forbidden_exaggerated_claims=("remote code execution",),
        ),
    )
    result = _tp_result("c1", _pred(impact="this leads to remote code execution on the server"))
    m = compute_security_quality_metrics([result], cases_by_id={"c1": case})
    assert m.impact_grounded_rate == 0.0
    assert m.unsupported_impact_rate == 1.0


def test_generic_advice_phrase_in_message_or_fix_is_flagged() -> None:
    case = _case("c1", _expected())
    result = _tp_result("c1", _pred(suggested_fix="just sanitize input before use"))
    m = compute_security_quality_metrics([result], cases_by_id={"c1": case})
    assert m.generic_advice_rate == 1.0


def test_unhedged_absolute_claim_on_low_confidence_finding_is_overclaiming() -> None:
    case = _case("c1", _expected())
    result = _tp_result("c1", _pred(confidence=Confidence.LOW, message="this allows arbitrary file access"))
    m = compute_security_quality_metrics([result], cases_by_id={"c1": case})
    assert m.low_or_medium_confidence_count == 1
    assert m.low_confidence_overclaim_rate == 1.0


def test_hedged_conditional_claim_on_low_confidence_finding_is_not_overclaiming() -> None:
    case = _case("c1", _expected())
    result = _tp_result(
        "c1",
        _pred(
            confidence=Confidence.LOW,
            message="if callers can supply `..` segments, this allows escaping the intended directory",
        ),
    )
    m = compute_security_quality_metrics([result], cases_by_id={"c1": case})
    assert m.low_confidence_overclaim_rate == 0.0


def test_high_confidence_finding_is_never_checked_for_overclaiming() -> None:
    case = _case("c1", _expected())
    result = _tp_result("c1", _pred(confidence=Confidence.HIGH, message="this allows arbitrary file access"))
    m = compute_security_quality_metrics([result], cases_by_id={"c1": case})
    assert m.low_or_medium_confidence_count == 0
    assert m.low_confidence_overclaim_rate == 0.0


def test_error_case_results_are_skipped() -> None:
    result = CaseResult(
        case_id="c1", mode=EvaluationMode.FULL_PIPELINE, status=CaseStatus.PROVIDER_ERROR, duration_ms=1.0,
        error="boom",
    )
    m = compute_security_quality_metrics([result], cases_by_id={"c1": _case("c1", _expected())})
    assert m.true_positives_scored == 0


def test_case_not_found_in_cases_by_id_is_skipped() -> None:
    result = _tp_result("unknown-case", _pred())
    m = compute_security_quality_metrics([result], cases_by_id={})
    assert m.true_positives_scored == 0
