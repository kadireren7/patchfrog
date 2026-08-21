"""Phase 8 spec section 57: the evaluation harness itself must scale.

Exercises ground-truth validation and metrics computation -- the two
pure, no-database layers that run once per case on every benchmark
run -- over 100 synthetic cases, and asserts the wall-clock stays
sane. Never runs 100 full production pipeline reviews (that would test
the reviewer's runtime, not the harness's own scalability, and would
make this a multi-minute unit test)."""

from __future__ import annotations

import time
from pathlib import Path

from patchfrog.analysis.domain import FindingCategory, Severity
from patchfrog.evaluation.domain import (
    CaseResult,
    CaseStatus,
    Difficulty,
    EvaluationCase,
    EvaluationMode,
    ExpectedFinding,
    ExpectedFindingOutcome,
    ExpectedOutcome,
    Language,
    MatchOutcome,
    PredictedFinding,
    PredictionOutcome,
    PredictionSource,
)
from patchfrog.evaluation.fixtures import validate_and_raise
from patchfrog.evaluation.metrics import compute_metrics

_CASE_COUNT = 100


def _build_synthetic_case_tree(root: Path, n: int) -> list[EvaluationCase]:
    cases = []
    for i in range(n):
        case_id = f"synthetic-{i:03d}"
        repo_root = root / case_id / "repo"
        repo_root.mkdir(parents=True)
        (repo_root / "m.py").write_text(f"def foo_{i}():\n    return {i}\n")
        case = EvaluationCase(
            id=case_id, title=f"synthetic case {i}", description="", language=Language.PYTHON, fixture=case_id,
            difficulty=Difficulty.EASY,
            expected=(
                ExpectedFinding(
                    id="ef1", category=FindingCategory.CORRECTNESS, file="m.py", issue_family="fam",
                    symbol=f"foo_{i}", severity=Severity.MEDIUM, line=2,
                ),
            ) if i % 2 == 0 else (),
        )
        cases.append(case)
    return cases


def test_ground_truth_validation_scales_to_100_cases(tmp_path: Path) -> None:
    cases = _build_synthetic_case_tree(tmp_path, _CASE_COUNT)
    start = time.monotonic()
    validate_and_raise(cases, cases_root=tmp_path)
    duration = time.monotonic() - start
    assert duration < 5.0, f"validating {_CASE_COUNT} cases took {duration:.2f}s -- investigate an N+1 or quadratic pattern"


def _synthetic_case_result(case: EvaluationCase, i: int) -> CaseResult:
    if not case.expected:
        return CaseResult(case_id=case.id, mode=EvaluationMode.FULL_PIPELINE, status=CaseStatus.PASSED, duration_ms=1.0)
    expected = case.expected[0]
    pred = PredictedFinding(
        source=PredictionSource.AI, category=expected.category, severity=Severity.MEDIUM, title="t", message="m",
        file_path=expected.file, start_line=2, end_line=2, symbol_qualified_name=expected.symbol, evidence_text="x",
    )
    outcome = PredictionOutcome(prediction=pred, outcome=MatchOutcome.TRUE_POSITIVE, matched_expected_id="ef1", detail="")
    expected_outcome = ExpectedFindingOutcome(expected=expected, outcome=ExpectedOutcome.FOUND, matched_prediction_index=0, detail="")
    return CaseResult(
        case_id=case.id, mode=EvaluationMode.FULL_PIPELINE, status=CaseStatus.COMPLETED_WITH_FINDINGS, duration_ms=1.0,
        predictions=(outcome,), expected_outcomes=(expected_outcome,), candidates_reviewed=1,
    )


def test_metrics_computation_scales_to_100_cases(tmp_path: Path) -> None:
    cases = _build_synthetic_case_tree(tmp_path, _CASE_COUNT)
    cases_by_id = {c.id: c for c in cases}
    results = [_synthetic_case_result(c, i) for i, c in enumerate(cases)]

    start = time.monotonic()
    metrics = compute_metrics(results, cases_by_id=cases_by_id, fixture_info={})
    duration = time.monotonic() - start

    assert duration < 2.0, f"computing metrics over {_CASE_COUNT} cases took {duration:.2f}s -- investigate an N+1 or quadratic pattern"
    assert metrics.overall.cases == _CASE_COUNT
    assert metrics.overall.confusion.true_positives == _CASE_COUNT // 2
    assert metrics.clean.clean_cases == _CASE_COUNT // 2
