from __future__ import annotations

from dataclasses import replace

from patchfrog.analysis.domain import Confidence, FindingCategory, Severity
from patchfrog.evaluation.beta_readiness import (
    BetaCaseExpectation,
    CriticExpectedOutcome,
    PublishabilityExpectation,
    compute_beta_readiness_metrics,
    load_beta_profile,
)
from patchfrog.evaluation.domain import (
    CaseResult,
    CaseStatus,
    EvaluationMode,
    MatchOutcome,
    PredictedFinding,
    PredictionOutcome,
    PredictionSource,
)
from patchfrog.evaluation.fixtures import DEFAULT_CASES_ROOT, load_all_cases, validate_and_raise


def _prediction(case_id: str, category: FindingCategory) -> PredictedFinding:
    return PredictedFinding(
        source=PredictionSource.AI,
        category=category,
        severity=Severity.HIGH,
        confidence=Confidence.HIGH,
        title=case_id,
        message="deterministic fixture finding",
        file_path="src/example.py",
        start_line=1,
        end_line=1,
        symbol_qualified_name="example",
        evidence_text="example",
    )


def _perfect_result(expectation: BetaCaseExpectation) -> CaseResult:
    case_id = expectation.case_id
    category = expectation.expected_category
    should_produce = expectation.should_produce_candidate
    should_publish = expectation.publishability is PublishabilityExpectation.PUBLISH
    assert category is not None or not should_produce
    proposal = _prediction(case_id, category) if should_produce and category is not None else None
    prediction = (
        PredictionOutcome(
            prediction=proposal,
            outcome=MatchOutcome.TRUE_POSITIVE,
            matched_expected_id="ef1",
            detail="matched",
        )
        if should_publish and proposal is not None
        else None
    )
    critic_calls = int(expectation.critic_expected_outcome is CriticExpectedOutcome.ACCEPT)
    return CaseResult(
        case_id=case_id,
        mode=EvaluationMode.AI_ONLY,
        status=CaseStatus.COMPLETED_WITH_FINDINGS if prediction else CaseStatus.PASSED,
        duration_ms=1.0,
        predictions=(prediction,) if prediction else (),
        proposals_before_validation=(proposal,) if proposal else (),
        proposal_outcomes=(prediction,) if prediction else (),
        critic_calls=critic_calls,
    )


def test_beta_profile_has_twenty_valid_explicit_cases() -> None:
    expectations = load_beta_profile()
    all_cases = load_all_cases()
    validate_and_raise(all_cases, cases_root=DEFAULT_CASES_ROOT)

    assert len(expectations) == 20
    assert {item.case_id for item in expectations} <= {case.id for case in all_cases}
    assert all(
        item.expected_category is not None and item.minimum_confidence is not None
        for item in expectations
        if item.should_produce_candidate
    )


def test_beta_metrics_cover_recall_false_positives_critic_and_variance() -> None:
    expectations = load_beta_profile()
    first = tuple(_perfect_result(item) for item in expectations)
    metrics = compute_beta_readiness_metrics([first, first], expectations=expectations)

    assert metrics.expectation_pass_rate == 1.0
    assert metrics.candidate_recall == 1.0
    assert metrics.accepted_finding_recall == 1.0
    assert metrics.false_positive_rate == 0.0
    assert metrics.false_negative_rate == 0.0
    assert metrics.critic_rejection_rate == 0.0
    assert metrics.critic_false_negative_rate == 0.0
    assert metrics.repeated_run_variance == 0.0

    changed = list(first)
    changed[0] = replace(changed[0], predictions=())
    varied = compute_beta_readiness_metrics([first, tuple(changed)], expectations=expectations)
    assert varied.repeated_run_variance == 1 / 20


def test_beta_metrics_expose_false_positive_and_critic_false_negative() -> None:
    expectations = load_beta_profile()
    results = [_perfect_result(item) for item in expectations]

    positive_index = next(
        index
        for index, item in enumerate(expectations)
        if item.publishability is PublishabilityExpectation.PUBLISH
    )
    positive = results[positive_index]
    results[positive_index] = replace(
        positive,
        predictions=(),
        critic_calls=1,
        critic_rejections=1,
    )

    negative_index = next(
        index
        for index, item in enumerate(expectations)
        if item.publishability is PublishabilityExpectation.DO_NOT_PUBLISH
    )
    false_positive = PredictionOutcome(
        prediction=_prediction(expectations[negative_index].case_id, FindingCategory.CORRECTNESS),
        outcome=MatchOutcome.FALSE_POSITIVE,
        matched_expected_id=None,
        detail="unexpected finding",
    )
    results[negative_index] = replace(
        results[negative_index],
        predictions=(false_positive,),
    )

    metrics = compute_beta_readiness_metrics([tuple(results)], expectations=expectations)

    assert metrics.false_positive_rate > 0
    assert metrics.false_negative_rate > 0
    assert metrics.critic_rejection_rate > 0
    assert metrics.critic_false_negative_rate > 0
    assert metrics.expectation_pass_rate < 1
