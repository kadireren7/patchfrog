"""Unit coverage for :mod:`patchfrog.evaluation.domain`'s own small pure
logic: ``ExpectedFinding.effective_line_range``/``severity_matches``,
``EvaluationCase.is_clean``, and ``CaseResult.is_error``."""

from __future__ import annotations

from patchfrog.analysis.domain import FindingCategory, Severity
from patchfrog.evaluation.domain import (
    CaseResult,
    CaseStatus,
    Difficulty,
    EvaluationCase,
    EvaluationMode,
    ExpectedFinding,
    Language,
    severity_level,
)


def _expected(
    *, severity: Severity | None = None, severity_min: Severity | None = None, severity_max: Severity | None = None,
    line: int | None = None, line_end: int | None = None, line_tolerance: int = 3,
) -> ExpectedFinding:
    return ExpectedFinding(
        id="ef1", category=FindingCategory.CORRECTNESS, file="a.py", issue_family="fam", severity=severity,
        severity_min=severity_min, severity_max=severity_max, line=line, line_end=line_end,
        line_tolerance=line_tolerance,
    )


def test_effective_line_range_none_when_no_line() -> None:
    assert _expected().effective_line_range is None


def test_effective_line_range_applies_tolerance_on_both_sides() -> None:
    rng = _expected(line=10, line_tolerance=2).effective_line_range
    assert rng == (8, 12)


def test_effective_line_range_uses_line_end_when_given() -> None:
    rng = _expected(line=10, line_end=15, line_tolerance=1).effective_line_range
    assert rng == (9, 16)


def test_severity_matches_exact() -> None:
    e = _expected(severity=Severity.HIGH)
    assert e.severity_matches(Severity.HIGH)
    assert not e.severity_matches(Severity.LOW)


def test_severity_matches_range() -> None:
    e = _expected(severity_min=Severity.MEDIUM, severity_max=Severity.HIGH)
    assert e.severity_matches(Severity.MEDIUM)
    assert e.severity_matches(Severity.HIGH)
    assert not e.severity_matches(Severity.LOW)
    assert not e.severity_matches(Severity.CRITICAL)


def test_severity_matches_permissive_when_unspecified() -> None:
    e = _expected()
    assert e.severity_matches(Severity.CRITICAL)
    assert e.severity_matches(Severity.INFO)


def test_severity_level_ordering() -> None:
    assert severity_level(Severity.INFO) < severity_level(Severity.LOW) < severity_level(Severity.MEDIUM)
    assert severity_level(Severity.MEDIUM) < severity_level(Severity.HIGH) < severity_level(Severity.CRITICAL)


def test_evaluation_case_is_clean_true_for_empty_expected() -> None:
    case = EvaluationCase(id="c1", title="t", description="d", language=Language.PYTHON, fixture="c1", difficulty=Difficulty.EASY)
    assert case.is_clean


def test_evaluation_case_is_clean_false_with_one_expected_finding() -> None:
    case = EvaluationCase(
        id="c1", title="t", description="d", language=Language.PYTHON, fixture="c1", difficulty=Difficulty.EASY,
        expected=(_expected(),),
    )
    assert not case.is_clean


def test_case_result_is_error_for_terminal_error_statuses() -> None:
    for status in (CaseStatus.TIMEOUT, CaseStatus.PROVIDER_ERROR, CaseStatus.FIXTURE_ERROR, CaseStatus.INFRASTRUCTURE_ERROR):
        result = CaseResult(case_id="c1", mode=EvaluationMode.FULL_PIPELINE, status=status, duration_ms=1.0)
        assert result.is_error, status


def test_case_result_is_not_error_for_passed_or_completed() -> None:
    for status in (CaseStatus.PASSED, CaseStatus.COMPLETED_WITH_FINDINGS):
        result = CaseResult(case_id="c1", mode=EvaluationMode.FULL_PIPELINE, status=status, duration_ms=1.0)
        assert not result.is_error, status
