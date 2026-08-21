"""Unit coverage for :mod:`patchfrog.evaluation.regression` -- identity
compatibility gating and the precision-over-recall regression
thresholds. Operates on plain dicts, exactly the shape
:mod:`patchfrog.evaluation.reporting` produces."""

from __future__ import annotations

from typing import Any

from patchfrog.evaluation.regression import RegressionThresholds, compare, identity_compatible

_BASE_IDENTITY: dict[str, Any] = {
    "evaluation_benchmark_version": 1,
    "evaluation_engine_version": 1,
    "review_engine_version": 1,
    "review_prompt_version": 1,
    "review_policy_version": 1,
    "incremental_review_engine_version": 1,
    "review_memory_version": 1,
    "mode": "full_pipeline",
}


def _metrics(*, precision: float, recall: float, clean_pass_rate: float = 1.0, unsupported: int = 0, duplicate_rate: float = 0.0) -> dict[str, Any]:
    return {
        "overall": {"scores": {"precision": precision, "recall": recall}, "duplicate_rate": duplicate_rate},
        "clean": {"pass_rate": clean_pass_rate},
        "hallucination": {"unsupported_after_validation": unsupported},
    }


def _report(*, identity: dict[str, Any] = _BASE_IDENTITY, **metric_kwargs: Any) -> dict[str, Any]:
    return {"identity": identity, "metrics": _metrics(**metric_kwargs)}


def test_identical_runs_are_compatible_and_pass() -> None:
    baseline = _report(precision=0.9, recall=0.8)
    current = _report(precision=0.9, recall=0.8)
    verdict = compare(baseline, current)
    assert verdict.identity_compatible
    assert verdict.passed
    assert verdict.exit_code == 0


def test_incompatible_identity_refuses_comparison_with_exit_code_2() -> None:
    baseline = _report(precision=0.9, recall=0.8)
    other_identity = {**_BASE_IDENTITY, "mode": "static_only"}
    current = _report(identity=other_identity, precision=0.9, recall=0.8)
    verdict = compare(baseline, current)
    assert not verdict.identity_compatible
    assert verdict.exit_code == 2
    assert verdict.identity_mismatch_detail is not None and "mode" in verdict.identity_mismatch_detail


def test_identity_compatible_ignores_non_gating_fields() -> None:
    # reviewer_provider/model are not in the must-match key list -- a
    # model swap is observable in the report but doesn't itself block
    # comparison (the caller decides whether that's meaningful).
    baseline = {**_BASE_IDENTITY, "reviewer_model": "model-a"}
    current = {**_BASE_IDENTITY, "reviewer_model": "model-b"}
    compatible, detail = identity_compatible(baseline, current)
    assert compatible and detail is None


def test_precision_drop_beyond_threshold_fails() -> None:
    baseline = _report(precision=0.95, recall=0.8)
    current = _report(precision=0.90, recall=0.8)  # 5-point drop > default 3-point threshold
    verdict = compare(baseline, current)
    precision_check = next(c for c in verdict.checks if c.name == "precision")
    assert not precision_check.passed
    assert not verdict.passed
    assert verdict.exit_code == 1


def test_small_precision_drop_within_threshold_passes() -> None:
    baseline = _report(precision=0.95, recall=0.8)
    current = _report(precision=0.93, recall=0.8)  # 2-point drop < 3-point threshold
    verdict = compare(baseline, current)
    precision_check = next(c for c in verdict.checks if c.name == "precision")
    assert precision_check.passed


def test_recall_has_a_looser_threshold_than_precision() -> None:
    # Same-magnitude drop: fails precision's tight threshold, passes
    # recall's default looser one -- this is the "precision > recall"
    # philosophy encoded directly.
    baseline = _report(precision=0.95, recall=0.90)
    current = _report(precision=0.90, recall=0.85)
    verdict = compare(baseline, current)
    precision_check = next(c for c in verdict.checks if c.name == "precision")
    recall_check = next(c for c in verdict.checks if c.name == "recall")
    assert not precision_check.passed
    assert recall_check.passed


def test_clean_case_pass_rate_regression() -> None:
    baseline = _report(precision=0.9, recall=0.8, clean_pass_rate=0.95)
    current = _report(precision=0.9, recall=0.8, clean_pass_rate=0.80)
    verdict = compare(baseline, current)
    check = next(c for c in verdict.checks if c.name == "clean_case_pass_rate")
    assert not check.passed


def test_unsupported_increase_fails_at_default_zero_threshold() -> None:
    baseline = _report(precision=0.9, recall=0.8, unsupported=0)
    current = _report(precision=0.9, recall=0.8, unsupported=1)
    verdict = compare(baseline, current)
    check = next(c for c in verdict.checks if c.name == "unsupported_accepted_findings")
    assert not check.passed
    assert not verdict.passed


def test_duplicate_rate_increase_regression() -> None:
    baseline = _report(precision=0.9, recall=0.8, duplicate_rate=0.0)
    current = _report(precision=0.9, recall=0.8, duplicate_rate=0.10)
    verdict = compare(baseline, current)
    check = next(c for c in verdict.checks if c.name == "duplicate_rate")
    assert not check.passed


def test_unsafe_carry_forward_present_fails_even_with_perfect_metrics() -> None:
    baseline = _report(precision=0.9, recall=0.8)
    current = _report(precision=0.9, recall=0.8)
    current["incremental"] = {"unsafe_carry_forward_count": 1}
    verdict = compare(baseline, current)
    check = next(c for c in verdict.checks if c.name == "unsafe_carry_forward")
    assert not check.passed
    assert not verdict.passed


def test_custom_thresholds_are_honored() -> None:
    baseline = _report(precision=0.95, recall=0.8)
    current = _report(precision=0.80, recall=0.8)  # 15-point drop
    strict = compare(baseline, current, thresholds=RegressionThresholds(max_precision_drop=0.20))
    assert strict.passed
    lenient_default = compare(baseline, current)
    assert not lenient_default.passed
