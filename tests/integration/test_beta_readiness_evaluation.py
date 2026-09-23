from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from patchfrog.evaluation.beta_readiness import (
    CriticExpectedOutcome,
    compute_beta_readiness_metrics,
    load_beta_profile,
)
from patchfrog.evaluation.domain import EvaluationMode, MatchOutcome
from patchfrog.evaluation.fixtures import DEFAULT_CASES_ROOT, load_all_cases
from patchfrog.evaluation.runner import EvaluationRunner, oracle_reviewer_provider_factory


async def test_beta_readiness_profile_is_deterministic_without_live_providers(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    expectations = load_beta_profile()
    expected_ids = {item.case_id for item in expectations}
    cases = [case for case in load_all_cases() if case.id in expected_ids]
    provider_factory = oracle_reviewer_provider_factory(cases_root=DEFAULT_CASES_ROOT)
    runner = EvaluationRunner(session_factory=session_factory)

    first = await runner.run_suite(
        cases,
        cases_root=DEFAULT_CASES_ROOT,
        mode=EvaluationMode.AI_ONLY,
        reviewer_provider_factory=provider_factory,
        critic_provider_factory=provider_factory,
        critic_enabled=True,
    )
    second = await runner.run_suite(
        cases,
        cases_root=DEFAULT_CASES_ROOT,
        mode=EvaluationMode.AI_ONLY,
        reviewer_provider_factory=provider_factory,
        critic_provider_factory=provider_factory,
        critic_enabled=True,
    )
    metrics = compute_beta_readiness_metrics([first, second], expectations=expectations)
    results_by_id = {result.case_id: result for result in first}
    failures: list[str] = []
    for expectation in expectations:
        result = results_by_id[expectation.case_id]
        proposal_match = any(
            outcome.outcome is MatchOutcome.TRUE_POSITIVE
            and outcome.prediction.category is expectation.expected_category
            for outcome in result.proposal_outcomes
        )
        accepted_match = any(
            outcome.outcome is MatchOutcome.TRUE_POSITIVE
            and outcome.prediction.category is expectation.expected_category
            for outcome in result.predictions
        )
        if proposal_match is not expectation.should_produce_candidate:
            failures.append(f"{expectation.case_id}: candidate={proposal_match}")
        should_publish = expectation.publishability.value == "publish"
        if accepted_match is not should_publish:
            failures.append(f"{expectation.case_id}: publishable={accepted_match}")
        if (
            expectation.critic_expected_outcome is CriticExpectedOutcome.ACCEPT
            and not (result.critic_calls > 0 and result.critic_rejections == 0 and accepted_match)
        ):
            failures.append(
                f"{expectation.case_id}: critic_calls={result.critic_calls}, "
                f"critic_rejections={result.critic_rejections}"
            )
        if (
            expectation.critic_expected_outcome is CriticExpectedOutcome.NOT_RUN
            and result.critic_calls != 0
        ):
            failures.append(f"{expectation.case_id}: critic_calls={result.critic_calls}")

    assert failures == []
    assert metrics.expectation_pass_rate == 1.0
    assert metrics.candidate_recall == 1.0
    assert metrics.accepted_finding_recall == 1.0
    assert metrics.false_positive_rate == 0.0
    assert metrics.false_negative_rate == 0.0
    assert metrics.critic_false_negative_rate == 0.0
    assert metrics.repeated_run_variance == 0.0
