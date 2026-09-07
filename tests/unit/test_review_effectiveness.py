"""Unit tests for the Review Effectiveness Benchmark foundation (Milestone
S, Part F) -- patchfrog.evaluation.review_effectiveness. Covers loader
correctness against the real corpus, duplicate-case-id detection, and
compute_metrics correctness for clean/non-clean cases."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from patchfrog.analysis.domain import FindingCategory, Severity
from patchfrog.evaluation.review_effectiveness import (
    DEFAULT_CORPUS_ROOT,
    ActualCaseOutcome,
    ReviewEffectivenessCase,
    ReviewEffectivenessGroundTruth,
    ScenarioCategory,
    compute_metrics,
    load_all_cases,
    load_case,
)


def _write_case(tmp_path: Path, filename: str, payload: dict[str, object]) -> Path:
    path = tmp_path / filename
    path.write_text(json.dumps(payload))
    return path


def test_load_case_parses_full_ground_truth(tmp_path: Path) -> None:
    path = _write_case(
        tmp_path,
        "case.json",
        {
            "case_id": "local_correctness_bug_001",
            "category": "local_correctness_bug",
            "description": "off-by-one",
            "ground_truth": {
                "expected_clean": False,
                "must_find_file_path": "src/pagination.py",
                "must_find_qualified_name": "paginate",
                "expected_finding_category": "correctness",
                "acceptable_severity_min": "medium",
                "acceptable_severity_max": "high",
            },
        },
    )
    case = load_case(path)
    assert case.case_id == "local_correctness_bug_001"
    assert case.category is ScenarioCategory.LOCAL_CORRECTNESS_BUG
    assert case.ground_truth.expected_clean is False
    assert case.ground_truth.must_find_file_path == "src/pagination.py"
    assert case.ground_truth.must_find_qualified_name == "paginate"
    assert case.ground_truth.expected_finding_category is FindingCategory.CORRECTNESS
    assert case.ground_truth.acceptable_severity_min is Severity.MEDIUM
    assert case.ground_truth.acceptable_severity_max is Severity.HIGH
    assert case.ground_truth.expected_execution_outcome is None


def test_load_case_parses_clean_case(tmp_path: Path) -> None:
    path = _write_case(
        tmp_path,
        "clean.json",
        {"case_id": "clean_pr_001", "category": "clean_pr", "description": "docstring only",
         "ground_truth": {"expected_clean": True}},
    )
    case = load_case(path)
    assert case.ground_truth.expected_clean is True
    assert case.ground_truth.must_find_file_path is None


def test_load_case_defaults_missing_ground_truth_to_not_clean(tmp_path: Path) -> None:
    path = _write_case(tmp_path, "no_gt.json", {"case_id": "x", "category": "clean_pr", "description": "d"})
    case = load_case(path)
    assert case.ground_truth == ReviewEffectivenessGroundTruth()
    assert case.ground_truth.expected_clean is False


def test_load_case_parses_execution_outcome(tmp_path: Path) -> None:
    path = _write_case(
        tmp_path,
        "exec.json",
        {
            "case_id": "executable_verification_case_001",
            "category": "executable_verification_case",
            "description": "confirmed failure",
            "ground_truth": {"expected_clean": False, "expected_execution_outcome": "confirmed_failure"},
        },
    )
    case = load_case(path)
    assert case.ground_truth.expected_execution_outcome == "confirmed_failure"


def test_load_all_cases_returns_empty_tuple_for_missing_directory(tmp_path: Path) -> None:
    assert load_all_cases(tmp_path / "does_not_exist") == ()


def test_load_all_cases_rejects_duplicate_case_id(tmp_path: Path) -> None:
    _write_case(tmp_path, "a.json", {"case_id": "dup", "category": "clean_pr", "description": "a"})
    _write_case(tmp_path, "b.json", {"case_id": "dup", "category": "clean_pr", "description": "b"})
    with pytest.raises(ValueError, match="duplicate review-effectiveness case_id"):
        load_all_cases(tmp_path)


def test_load_all_cases_loads_real_corpus() -> None:
    cases = load_all_cases(DEFAULT_CORPUS_ROOT)
    assert len(cases) >= 3
    case_ids = {c.case_id for c in cases}
    assert "clean_pr_001" in case_ids
    assert "local_correctness_bug_001" in case_ids
    assert "executable_verification_case_001" in case_ids


def _case(case_id: str, *, expected_clean: bool) -> ReviewEffectivenessCase:
    return ReviewEffectivenessCase(
        case_id=case_id,
        category=ScenarioCategory.CLEAN_PR if expected_clean else ScenarioCategory.LOCAL_CORRECTNESS_BUG,
        description="d",
        ground_truth=ReviewEffectivenessGroundTruth(expected_clean=expected_clean),
    )


def test_compute_metrics_clean_case_precision() -> None:
    clean = _case("clean_1", expected_clean=True)
    outcomes = {
        "clean_1": ActualCaseOutcome(
            case_id="clean_1", findings_reported=0, matched_must_find=False,
            matched_must_not_find_violation=False, duplicate_findings=0,
        ),
    }
    metrics = compute_metrics((clean,), outcomes)
    assert metrics.clean_case_count == 1
    assert metrics.non_clean_case_count == 0
    assert metrics.clean_case_precision == 1.0
    assert metrics.total_false_positives_on_clean_cases == 0
    assert metrics.case_recall is None


def test_compute_metrics_clean_case_with_false_positive() -> None:
    clean = _case("clean_1", expected_clean=True)
    outcomes = {
        "clean_1": ActualCaseOutcome(
            case_id="clean_1", findings_reported=2, matched_must_find=False,
            matched_must_not_find_violation=False, duplicate_findings=0,
        ),
    }
    metrics = compute_metrics((clean,), outcomes)
    assert metrics.clean_case_precision == 0.0
    assert metrics.total_false_positives_on_clean_cases == 2


def test_compute_metrics_non_clean_case_recall() -> None:
    bug = _case("bug_1", expected_clean=False)
    outcomes = {
        "bug_1": ActualCaseOutcome(
            case_id="bug_1", findings_reported=1, matched_must_find=True,
            matched_must_not_find_violation=False, duplicate_findings=0,
        ),
    }
    metrics = compute_metrics((bug,), outcomes)
    assert metrics.case_recall == 1.0
    assert metrics.clean_case_precision is None


def test_compute_metrics_missed_recall() -> None:
    bug = _case("bug_1", expected_clean=False)
    outcomes = {
        "bug_1": ActualCaseOutcome(
            case_id="bug_1", findings_reported=0, matched_must_find=False,
            matched_must_not_find_violation=False, duplicate_findings=0,
        ),
    }
    metrics = compute_metrics((bug,), outcomes)
    assert metrics.case_recall == 0.0


def test_compute_metrics_case_with_no_outcome_excluded_not_defaulted() -> None:
    bug = _case("bug_1", expected_clean=False)
    clean = _case("clean_1", expected_clean=True)
    metrics = compute_metrics((bug, clean), {})
    assert metrics.case_count == 2
    assert metrics.case_recall is None
    assert metrics.clean_case_precision is None
    assert metrics.total_false_positives_on_clean_cases == 0


def test_compute_metrics_duplicate_and_violation_counts() -> None:
    bug = _case("bug_1", expected_clean=False)
    clean = _case("clean_1", expected_clean=True)
    outcomes = {
        "bug_1": ActualCaseOutcome(
            case_id="bug_1", findings_reported=2, matched_must_find=True,
            matched_must_not_find_violation=True, duplicate_findings=1,
        ),
        "clean_1": ActualCaseOutcome(
            case_id="clean_1", findings_reported=0, matched_must_find=False,
            matched_must_not_find_violation=False, duplicate_findings=1,
        ),
    }
    metrics = compute_metrics((bug, clean), outcomes)
    assert metrics.total_duplicate_findings == 2
    assert metrics.total_must_not_find_violations == 1


def test_compute_metrics_mixed_recall_and_precision() -> None:
    bug_found = _case("bug_found", expected_clean=False)
    bug_missed = _case("bug_missed", expected_clean=False)
    clean_ok = _case("clean_ok", expected_clean=True)
    outcomes = {
        "bug_found": ActualCaseOutcome(
            case_id="bug_found", findings_reported=1, matched_must_find=True,
            matched_must_not_find_violation=False, duplicate_findings=0,
        ),
        "bug_missed": ActualCaseOutcome(
            case_id="bug_missed", findings_reported=0, matched_must_find=False,
            matched_must_not_find_violation=False, duplicate_findings=0,
        ),
        "clean_ok": ActualCaseOutcome(
            case_id="clean_ok", findings_reported=0, matched_must_find=False,
            matched_must_not_find_violation=False, duplicate_findings=0,
        ),
    }
    metrics = compute_metrics((bug_found, bug_missed, clean_ok), outcomes)
    assert metrics.case_recall == 0.5
    assert metrics.clean_case_precision == 1.0
    assert metrics.case_count == 3
