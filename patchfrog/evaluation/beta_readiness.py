"""Deterministic beta-readiness evaluation profile and metrics."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

import yaml

from patchfrog.analysis.domain import Confidence, FindingCategory
from patchfrog.evaluation.domain import CaseResult, MatchOutcome

DEFAULT_BETA_PROFILE = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "evaluation" / "beta_readiness.yaml"


class CriticExpectedOutcome(StrEnum):
    ACCEPT = "accept"
    NOT_REQUIRED = "not_required"
    NOT_RUN = "not_run"


class PublishabilityExpectation(StrEnum):
    PUBLISH = "publish"
    DO_NOT_PUBLISH = "do_not_publish"


@dataclass(frozen=True, slots=True)
class BetaCaseExpectation:
    case_id: str
    should_produce_candidate: bool
    expected_category: FindingCategory | None
    minimum_confidence: Confidence | None
    critic_expected_outcome: CriticExpectedOutcome
    publishability: PublishabilityExpectation


@dataclass(frozen=True, slots=True)
class BetaReadinessMetrics:
    cases: int
    expectation_pass_rate: float
    candidate_recall: float
    accepted_finding_recall: float
    false_positive_rate: float
    false_negative_rate: float
    critic_rejection_rate: float
    critic_false_negative_rate: float
    repeated_run_variance: float


def load_beta_profile(path: Path = DEFAULT_BETA_PROFILE) -> tuple[BetaCaseExpectation, ...]:
    raw = yaml.safe_load(path.read_text())
    rows = raw.get("cases", []) if isinstance(raw, dict) else []
    expectations = tuple(
        BetaCaseExpectation(
            case_id=str(row["case_id"]),
            should_produce_candidate=bool(row["should_produce_candidate"]),
            expected_category=(
                FindingCategory(row["expected_category"])
                if row.get("expected_category") is not None
                else None
            ),
            minimum_confidence=(
                Confidence(row["minimum_confidence"])
                if row.get("minimum_confidence") is not None
                else None
            ),
            critic_expected_outcome=CriticExpectedOutcome(row["critic_expected_outcome"]),
            publishability=PublishabilityExpectation(row["publishability"]),
        )
        for row in rows
    )
    if not 15 <= len(expectations) <= 20:
        raise ValueError(f"beta-readiness profile must contain 15-20 cases, found {len(expectations)}")
    ids = [item.case_id for item in expectations]
    if len(set(ids)) != len(ids):
        raise ValueError("beta-readiness profile contains duplicate case ids")
    return expectations


def compute_beta_readiness_metrics(
    runs: Sequence[Sequence[CaseResult]],
    *,
    expectations: Sequence[BetaCaseExpectation],
) -> BetaReadinessMetrics:
    if not runs:
        raise ValueError("at least one evaluation run is required")
    expected_by_id = {item.case_id: item for item in expectations}
    first = {result.case_id: result for result in runs[0]}
    if set(first) != set(expected_by_id):
        raise ValueError("evaluation results do not match the beta-readiness profile")

    candidate_expected = sum(item.should_produce_candidate for item in expectations)
    publish_expected = sum(item.publishability is PublishabilityExpectation.PUBLISH for item in expectations)
    candidate_hits = accepted_hits = false_positives = true_positives = missed = 0
    critic_calls = critic_rejections = critic_false_negatives = 0
    expectations_passed = 0

    for case_id, expectation in expected_by_id.items():
        result = first[case_id]
        proposal_matches = any(
            outcome.outcome is MatchOutcome.TRUE_POSITIVE
            and outcome.prediction.category is expectation.expected_category
            for outcome in result.proposal_outcomes
        )
        if expectation.should_produce_candidate and proposal_matches:
            candidate_hits += 1

        accepted_match = any(
            outcome.outcome is MatchOutcome.TRUE_POSITIVE
            and outcome.prediction.category is expectation.expected_category
            and (
                expectation.minimum_confidence is None
                or (outcome.prediction.confidence is not None
                and _confidence_rank(outcome.prediction.confidence)
                >= _confidence_rank(expectation.minimum_confidence))
            )
            for outcome in result.predictions
        )
        if expectation.publishability is PublishabilityExpectation.PUBLISH:
            if accepted_match:
                accepted_hits += 1
                true_positives += 1
            else:
                missed += 1
                if proposal_matches and result.critic_rejections:
                    critic_false_negatives += 1
        else:
            false_positives += sum(
                outcome.outcome in (MatchOutcome.FALSE_POSITIVE, MatchOutcome.UNSUPPORTED)
                for outcome in result.predictions
            )
        candidate_ok = (
            proposal_matches
            if expectation.should_produce_candidate
            else not result.proposals_before_validation
        )
        publish_ok = accepted_match is (
            expectation.publishability is PublishabilityExpectation.PUBLISH
        )
        if expectation.critic_expected_outcome is CriticExpectedOutcome.ACCEPT:
            critic_ok = result.critic_calls > 0 and result.critic_rejections == 0 and accepted_match
        elif expectation.critic_expected_outcome is CriticExpectedOutcome.NOT_RUN:
            critic_ok = result.critic_calls == 0
        else:
            critic_ok = True
        expectations_passed += candidate_ok and publish_ok and critic_ok
        critic_calls += result.critic_calls
        critic_rejections += result.critic_rejections

    return BetaReadinessMetrics(
        cases=len(expectations),
        expectation_pass_rate=_ratio(expectations_passed, len(expectations)),
        candidate_recall=_ratio(candidate_hits, candidate_expected),
        accepted_finding_recall=_ratio(accepted_hits, publish_expected),
        false_positive_rate=_ratio(false_positives, true_positives + false_positives),
        false_negative_rate=_ratio(missed, true_positives + missed),
        critic_rejection_rate=_ratio(critic_rejections, critic_calls),
        critic_false_negative_rate=_ratio(critic_false_negatives, publish_expected),
        repeated_run_variance=_repeated_run_variance(runs),
    )


def _confidence_rank(confidence: Confidence) -> int:
    return {Confidence.LOW: 0, Confidence.MEDIUM: 1, Confidence.HIGH: 2}[confidence]


def _ratio(numerator: int, denominator: int) -> float:
    return numerator / denominator if denominator else 0.0


def _result_signature(result: CaseResult) -> tuple[object, ...]:
    return (
        result.status,
        tuple(
            (p.outcome, p.prediction.category, p.prediction.file_path, p.prediction.start_line)
            for p in result.predictions
        ),
        tuple((p.category, p.file_path, p.start_line) for p in result.proposals_before_validation),
        tuple(p.outcome for p in result.proposal_outcomes),
        result.critic_calls,
        result.critic_rejections,
    )


def _repeated_run_variance(runs: Sequence[Sequence[CaseResult]]) -> float:
    if len(runs) < 2:
        return 0.0
    by_run: list[Mapping[str, CaseResult]] = [
        {result.case_id: result for result in run} for run in runs
    ]
    case_ids = set(by_run[0])
    varied = sum(
        len({_result_signature(run[case_id]) for run in by_run}) > 1
        for case_id in case_ids
    )
    return _ratio(varied, len(case_ids))


__all__ = [
    "DEFAULT_BETA_PROFILE",
    "BetaCaseExpectation",
    "BetaReadinessMetrics",
    "CriticExpectedOutcome",
    "PublishabilityExpectation",
    "compute_beta_readiness_metrics",
    "load_beta_profile",
]
