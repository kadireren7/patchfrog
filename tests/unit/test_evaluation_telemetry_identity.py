"""Evaluation & Telemetry Intelligence milestone: identity/comparison/
regression-threshold tests (spec sections 18/19/36).

No database, no LLM -- :func:`~patchfrog.evaluation.runner.build_evaluation_identity`
only needs a provider's ``.identity`` and an empty case list.
"""

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path

from patchfrog.context.config import AdaptiveContextConfig, ContextConfig
from patchfrog.evaluation.domain import EVALUATION_ENGINE_VERSION, EvaluationMode
from patchfrog.evaluation.regression import RegressionThresholds, compare, identity_compatible
from patchfrog.evaluation.runner import build_evaluation_identity
from patchfrog.review.config import QUALITY_COST_POLICY_VERSION
from patchfrog.review.providers.fake import FakeLLMProvider


def _identity(**kwargs: object) -> dict[str, object]:
    identity = build_evaluation_identity(
        mode=EvaluationMode.FULL_PIPELINE, reviewer_provider=FakeLLMProvider(), critic_enabled=True,
        cases=[], cases_root=Path("."), **kwargs,  # type: ignore[arg-type]
    )
    return asdict(identity)


def test_evaluation_engine_version_is_2() -> None:
    assert EVALUATION_ENGINE_VERSION == 2


def test_quality_cost_policy_version_participates_in_identity() -> None:
    identity = _identity()
    assert identity["quality_cost_policy_version"] == QUALITY_COST_POLICY_VERSION


def test_context_engine_version_participates_in_identity() -> None:
    from patchfrog.context.config import CONTEXT_ENGINE_VERSION

    identity = _identity()
    assert identity["context_engine_version"] == CONTEXT_ENGINE_VERSION


def test_guard_on_vs_uniform_identity_differs() -> None:
    guard_on = _identity(use_quality_cost_guard=True)
    uniform = _identity(use_quality_cost_guard=False)
    assert guard_on["quality_cost_guard_enabled"] is True
    assert uniform["quality_cost_guard_enabled"] is False
    compatible, detail = identity_compatible(guard_on, uniform)
    assert not compatible, detail


def test_fixed_depth_1_vs_adaptive_identity_differs() -> None:
    fixed1 = _identity(context_config_override=ContextConfig())
    adaptive = _identity(context_config_override=ContextConfig(adaptive=AdaptiveContextConfig(enabled=True)))
    assert fixed1["context_config_identity"] != adaptive["context_config_identity"]
    compatible, detail = identity_compatible(fixed1, adaptive)
    assert not compatible, detail


def test_fixed_depth_1_vs_fixed_depth_2_identity_differs() -> None:
    fixed1 = _identity(context_config_override=ContextConfig())
    fixed2 = _identity(context_config_override=ContextConfig(graph_depth=2))
    assert fixed1["context_config_identity"] != fixed2["context_config_identity"]


def test_default_context_config_identity_is_the_literal_default_sentinel() -> None:
    identity = _identity()
    assert identity["context_config_identity"] == "default"


def _report(*, precision: float, recall: float, provider_calls: int = 100, input_tokens: int = 1000, critic_calls: int = 20) -> dict[str, object]:
    identity = _identity()
    return {
        "identity": identity,
        "metrics": {
            "overall": {"scores": {"precision": precision, "recall": recall}, "duplicate_rate": 0.0},
            "clean": {"pass_rate": 1.0},
            "hallucination": {"unsupported_after_validation": 0},
            "efficiency": {"provider_calls": provider_calls, "reviewer_input_tokens": input_tokens, "critic_calls": critic_calls},
        },
    }


def test_compatible_run_comparison_works() -> None:
    baseline = _report(precision=0.9, recall=0.8)
    current = _report(precision=0.9, recall=0.8)
    verdict = compare(baseline, current)
    assert verdict.identity_compatible
    assert verdict.passed
    assert verdict.exit_code == 0


def test_incompatible_run_comparison_refuses() -> None:
    baseline = _report(precision=0.9, recall=0.8)
    current_identity = _identity(use_quality_cost_guard=False)
    current = _report(precision=0.9, recall=0.8)
    current["identity"] = current_identity
    verdict = compare(baseline, current)
    assert not verdict.identity_compatible
    assert not verdict.passed
    assert verdict.exit_code == 2


def test_quality_delta_calculation() -> None:
    baseline = _report(precision=0.9, recall=0.8)
    current = _report(precision=0.80, recall=0.8)  # 0.10 precision drop -- exceeds default 0.03 threshold
    verdict = compare(baseline, current)
    precision_check = next(c for c in verdict.checks if c.name == "precision")
    assert not precision_check.passed
    assert not verdict.passed


def test_cost_delta_calculation_is_report_only_by_default() -> None:
    baseline = _report(precision=0.9, recall=0.8, provider_calls=100)
    current = _report(precision=0.9, recall=0.8, provider_calls=500)  # 5x increase
    verdict = compare(baseline, current)
    cost_check = next(c for c in verdict.checks if c.name == "cost_provider_calls")
    # Default threshold is None -- report-only: never fails the run.
    assert cost_check.passed
    assert "400.00%" in cost_check.detail
    assert verdict.passed


def test_configured_cost_threshold_actually_fails() -> None:
    baseline = _report(precision=0.9, recall=0.8, provider_calls=100)
    current = _report(precision=0.9, recall=0.8, provider_calls=500)
    thresholds = RegressionThresholds(max_provider_calls_increase_pct=10.0)
    verdict = compare(baseline, current, thresholds=thresholds)
    cost_check = next(c for c in verdict.checks if c.name == "cost_provider_calls")
    assert not cost_check.passed
    assert not verdict.passed


def test_efficiency_delta_calculation_for_input_tokens_and_critic_calls() -> None:
    baseline = _report(precision=0.9, recall=0.8, input_tokens=1000, critic_calls=20)
    current = _report(precision=0.9, recall=0.8, input_tokens=1200, critic_calls=25)
    verdict = compare(baseline, current)
    token_check = next(c for c in verdict.checks if c.name == "cost_input_tokens")
    critic_check = next(c for c in verdict.checks if c.name == "cost_critic_calls")
    assert "20.00%" in token_check.detail
    assert "25.00%" in critic_check.detail


def test_regression_thresholds_default_to_report_only_for_all_three_cost_checks() -> None:
    thresholds = RegressionThresholds()
    assert thresholds.max_provider_calls_increase_pct is None
    assert thresholds.max_input_tokens_increase_pct is None
    assert thresholds.max_critic_calls_increase_pct is None
