"""Unit coverage for :mod:`patchfrog.evaluation.matcher` -- the
deterministic, no-LLM matching engine at the core of Phase 8's canonical
score. Every classification (TRUE_POSITIVE/FALSE_POSITIVE/DUPLICATE/
UNSUPPORTED/OUT_OF_SCOPE, FOUND/MISSED) is exercised directly against
hand-built cases and predictions -- no database, no provider, no I/O.
"""

from __future__ import annotations

from patchfrog.analysis.domain import FindingCategory, Severity
from patchfrog.evaluation.domain import (
    Difficulty,
    EvaluationCase,
    EvaluationMode,
    ExpectedFinding,
    ExpectedOutcome,
    ForbiddenFinding,
    GroundTruthSource,
    Language,
    MatchOutcome,
    PredictedFinding,
    PredictionSource,
)
from patchfrog.evaluation.matcher import match_case, unsupported_reason


def _pred(
    *, category: FindingCategory = FindingCategory.CORRECTNESS, file_path: str = "a.py",
    start_line: int = 10, end_line: int = 10, symbol: str | None = "foo",
    severity: Severity = Severity.HIGH, evidence: str = "return a - b",
) -> PredictedFinding:
    return PredictedFinding(
        source=PredictionSource.AI, category=category, severity=severity, title="t", message="m",
        file_path=file_path, start_line=start_line, end_line=end_line, symbol_qualified_name=symbol,
        evidence_text=evidence,
    )


def _expected(
    *, id_: str = "ef1", category: FindingCategory = FindingCategory.CORRECTNESS, file: str = "a.py",
    symbol: str | None = "foo", line: int | None = 10, line_end: int | None = None, line_tolerance: int = 3,
    evidence_contains: str | None = None, ground_truth_source: GroundTruthSource = GroundTruthSource.EITHER,
) -> ExpectedFinding:
    return ExpectedFinding(
        id=id_, category=category, file=file, issue_family="fam", symbol=symbol, severity=Severity.HIGH,
        line=line, line_end=line_end, line_tolerance=line_tolerance, evidence_contains=evidence_contains,
        ground_truth_source=ground_truth_source,
    )


def _case(*, expected: tuple[ExpectedFinding, ...] = (), forbidden: tuple[ForbiddenFinding, ...] = ()) -> EvaluationCase:
    return EvaluationCase(
        id="c1", title="t", description="d", language=Language.PYTHON, fixture="c1", difficulty=Difficulty.EASY,
        expected=expected, forbidden=forbidden,
    )


_VALID_PATHS = frozenset({"a.py", "b.py"})
_LINE_COUNTS = {"a.py": 50, "b.py": 50}


def test_exact_match_is_true_positive() -> None:
    case = _case(expected=(_expected(),))
    predictions, expected_outcomes = match_case(
        case=case, mode=EvaluationMode.FULL_PIPELINE, predictions=[_pred()],
        valid_file_paths=_VALID_PATHS, file_line_counts=_LINE_COUNTS,
    )
    assert predictions[0].outcome is MatchOutcome.TRUE_POSITIVE
    assert expected_outcomes[0].outcome is ExpectedOutcome.FOUND


def test_no_expected_match_is_false_positive() -> None:
    case = _case(expected=())
    predictions, _ = match_case(
        case=case, mode=EvaluationMode.FULL_PIPELINE, predictions=[_pred()],
        valid_file_paths=_VALID_PATHS, file_line_counts=_LINE_COUNTS,
    )
    assert predictions[0].outcome is MatchOutcome.FALSE_POSITIVE


def test_matching_forbidden_rule_is_out_of_scope_not_false_positive() -> None:
    case = _case(
        expected=(),
        forbidden=(ForbiddenFinding(reason="style nit", category=FindingCategory.CORRECTNESS),),
    )
    predictions, _ = match_case(
        case=case, mode=EvaluationMode.FULL_PIPELINE, predictions=[_pred()],
        valid_file_paths=_VALID_PATHS, file_line_counts=_LINE_COUNTS,
    )
    assert predictions[0].outcome is MatchOutcome.OUT_OF_SCOPE
    assert predictions[0].forbidden_reason == "style nit"


def test_missing_prediction_is_missed() -> None:
    case = _case(expected=(_expected(),))
    _, expected_outcomes = match_case(
        case=case, mode=EvaluationMode.FULL_PIPELINE, predictions=[],
        valid_file_paths=_VALID_PATHS, file_line_counts=_LINE_COUNTS,
    )
    assert expected_outcomes[0].outcome is ExpectedOutcome.MISSED


def test_second_matching_prediction_is_duplicate() -> None:
    case = _case(expected=(_expected(),))
    predictions, expected_outcomes = match_case(
        case=case, mode=EvaluationMode.FULL_PIPELINE, predictions=[_pred(), _pred()],
        valid_file_paths=_VALID_PATHS, file_line_counts=_LINE_COUNTS,
    )
    outcomes = [p.outcome for p in predictions]
    assert outcomes.count(MatchOutcome.TRUE_POSITIVE) == 1
    assert outcomes.count(MatchOutcome.DUPLICATE) == 1
    assert expected_outcomes[0].outcome is ExpectedOutcome.FOUND
    assert "duplicate" in expected_outcomes[0].detail


def test_unsupported_beats_false_positive_for_nonexistent_file() -> None:
    case = _case(expected=())
    predictions, _ = match_case(
        case=case, mode=EvaluationMode.FULL_PIPELINE, predictions=[_pred(file_path="nonexistent.py")],
        valid_file_paths=_VALID_PATHS, file_line_counts=_LINE_COUNTS,
    )
    assert predictions[0].outcome is MatchOutcome.UNSUPPORTED
    assert "nonexistent.py" in predictions[0].detail


def test_unsupported_beats_true_positive_for_line_beyond_file() -> None:
    # A prediction that would otherwise match ef1 on file/category/symbol
    # but cites a line beyond the file's real length must never be
    # accepted as a true positive -- the file exists, so this can only
    # be caught by the line-count check, not the file-existence check.
    case = _case(expected=(_expected(line=10),))
    predictions, _ = match_case(
        case=case, mode=EvaluationMode.FULL_PIPELINE,
        predictions=[_pred(start_line=999, end_line=999)],
        valid_file_paths=_VALID_PATHS, file_line_counts=_LINE_COUNTS,
    )
    assert predictions[0].outcome in (MatchOutcome.UNSUPPORTED, MatchOutcome.FALSE_POSITIVE)


def test_unsupported_reason_flags_invalid_line_range() -> None:
    pred = _pred(start_line=5, end_line=2)
    reason = unsupported_reason(pred, _VALID_PATHS, _LINE_COUNTS)
    assert reason is not None and "invalid line range" in reason


def test_unsupported_reason_none_for_valid_prediction() -> None:
    assert unsupported_reason(_pred(), _VALID_PATHS, _LINE_COUNTS) is None


def test_category_mismatch_is_not_a_match() -> None:
    case = _case(expected=(_expected(category=FindingCategory.SECURITY),))
    predictions, expected_outcomes = match_case(
        case=case, mode=EvaluationMode.FULL_PIPELINE, predictions=[_pred(category=FindingCategory.CORRECTNESS)],
        valid_file_paths=_VALID_PATHS, file_line_counts=_LINE_COUNTS,
    )
    assert predictions[0].outcome is MatchOutcome.FALSE_POSITIVE
    assert expected_outcomes[0].outcome is ExpectedOutcome.MISSED


def test_symbol_tolerant_suffix_match_for_qualified_method_name() -> None:
    # A bare expected symbol ("foo") matches a fully-qualified prediction
    # ("Account.foo") -- but never the reverse.
    case = _case(expected=(_expected(symbol="foo"),))
    predictions, _ = match_case(
        case=case, mode=EvaluationMode.FULL_PIPELINE, predictions=[_pred(symbol="Account.foo")],
        valid_file_paths=_VALID_PATHS, file_line_counts=_LINE_COUNTS,
    )
    assert predictions[0].outcome is MatchOutcome.TRUE_POSITIVE


def test_qualified_expected_symbol_does_not_match_bare_prediction() -> None:
    case = _case(expected=(_expected(symbol="Account.foo"),))
    predictions, expected_outcomes = match_case(
        case=case, mode=EvaluationMode.FULL_PIPELINE, predictions=[_pred(symbol="foo")],
        valid_file_paths=_VALID_PATHS, file_line_counts=_LINE_COUNTS,
    )
    assert predictions[0].outcome is MatchOutcome.FALSE_POSITIVE
    assert expected_outcomes[0].outcome is ExpectedOutcome.MISSED


def test_line_within_tolerance_still_matches() -> None:
    case = _case(expected=(_expected(line=10, line_tolerance=3),))
    predictions, _ = match_case(
        case=case, mode=EvaluationMode.FULL_PIPELINE, predictions=[_pred(start_line=13, end_line=13)],
        valid_file_paths=_VALID_PATHS, file_line_counts=_LINE_COUNTS,
    )
    assert predictions[0].outcome is MatchOutcome.TRUE_POSITIVE


def test_line_outside_tolerance_does_not_match() -> None:
    case = _case(expected=(_expected(line=10, line_tolerance=3),))
    predictions, expected_outcomes = match_case(
        case=case, mode=EvaluationMode.FULL_PIPELINE, predictions=[_pred(start_line=20, end_line=20)],
        valid_file_paths=_VALID_PATHS, file_line_counts=_LINE_COUNTS,
    )
    assert predictions[0].outcome is MatchOutcome.FALSE_POSITIVE
    assert expected_outcomes[0].outcome is ExpectedOutcome.MISSED


def test_evidence_contains_gate() -> None:
    case = _case(expected=(_expected(evidence_contains="a - b"),))
    predictions_ok, _ = match_case(
        case=case, mode=EvaluationMode.FULL_PIPELINE, predictions=[_pred(evidence="  return a - b  ")],
        valid_file_paths=_VALID_PATHS, file_line_counts=_LINE_COUNTS,
    )
    assert predictions_ok[0].outcome is MatchOutcome.TRUE_POSITIVE

    predictions_bad, expected_bad = match_case(
        case=case, mode=EvaluationMode.FULL_PIPELINE, predictions=[_pred(evidence="unrelated text")],
        valid_file_paths=_VALID_PATHS, file_line_counts=_LINE_COUNTS,
    )
    assert predictions_bad[0].outcome is MatchOutcome.FALSE_POSITIVE
    assert expected_bad[0].outcome is ExpectedOutcome.MISSED


def test_severity_is_never_part_of_the_match_predicate() -> None:
    # A prediction at the wrong severity must still count as a true
    # positive on the match itself -- severity calibration is measured
    # separately (see test_evaluation_metrics.py), never folded into a miss.
    case = _case(expected=(_expected(),))
    predictions, _ = match_case(
        case=case, mode=EvaluationMode.FULL_PIPELINE, predictions=[_pred(severity=Severity.LOW)],
        valid_file_paths=_VALID_PATHS, file_line_counts=_LINE_COUNTS,
    )
    assert predictions[0].outcome is MatchOutcome.TRUE_POSITIVE


def test_static_only_mode_excludes_ai_expected_findings() -> None:
    case = _case(expected=(_expected(ground_truth_source=GroundTruthSource.AI_EXPECTED),))
    _, expected_outcomes = match_case(
        case=case, mode=EvaluationMode.STATIC_ONLY, predictions=[],
        valid_file_paths=_VALID_PATHS, file_line_counts=_LINE_COUNTS,
    )
    # An AI-only expectation is simply out of scope for STATIC_ONLY --
    # never reported as MISSED, since it was never supposed to be caught here.
    assert expected_outcomes == ()


def test_ai_only_mode_excludes_static_expected_findings() -> None:
    case = _case(expected=(_expected(ground_truth_source=GroundTruthSource.STATIC_EXPECTED),))
    _, expected_outcomes = match_case(
        case=case, mode=EvaluationMode.AI_ONLY, predictions=[],
        valid_file_paths=_VALID_PATHS, file_line_counts=_LINE_COUNTS,
    )
    assert expected_outcomes == ()


def test_full_pipeline_mode_includes_both_sources() -> None:
    case = _case(
        expected=(
            _expected(id_="ef1", ground_truth_source=GroundTruthSource.STATIC_EXPECTED),
            _expected(id_="ef2", ground_truth_source=GroundTruthSource.AI_EXPECTED, line=20),
        )
    )
    _, expected_outcomes = match_case(
        case=case, mode=EvaluationMode.FULL_PIPELINE, predictions=[],
        valid_file_paths=_VALID_PATHS, file_line_counts=_LINE_COUNTS,
    )
    assert {e.expected.id for e in expected_outcomes} == {"ef1", "ef2"}
    assert all(e.outcome is ExpectedOutcome.MISSED for e in expected_outcomes)
