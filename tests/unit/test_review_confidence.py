from __future__ import annotations

import pytest

from patchfrog.analysis.domain import Confidence, Severity
from patchfrog.review.confidence import aggregate, meets_minimum
from patchfrog.review.domain import CriticDecision, CriticVerdict


def _verdict(decision: CriticDecision, **overrides: object) -> CriticVerdict:
    kwargs: dict[str, object] = {"decision": decision, "reasoning_summary": "x"}
    kwargs.update(overrides)
    return CriticVerdict(**kwargs)  # type: ignore[arg-type]


def test_no_critic_keeps_reviewer_values() -> None:
    result = aggregate(
        reviewer_confidence=Confidence.MEDIUM,
        reviewer_severity=Severity.HIGH,
        critic_verdict=None,
        corroborated_by_static=False,
    )
    assert result.final_confidence == Confidence.MEDIUM
    assert result.final_severity == Severity.HIGH
    assert result.corroborated_by_static is False


def test_accept_verdict_keeps_reviewer_values() -> None:
    result = aggregate(
        reviewer_confidence=Confidence.HIGH,
        reviewer_severity=Severity.CRITICAL,
        critic_verdict=_verdict(CriticDecision.ACCEPT),
        corroborated_by_static=False,
    )
    assert result.final_confidence == Confidence.HIGH
    assert result.final_severity == Severity.CRITICAL


def test_downgrade_verdict_overrides_severity_and_confidence() -> None:
    result = aggregate(
        reviewer_confidence=Confidence.HIGH,
        reviewer_severity=Severity.CRITICAL,
        critic_verdict=_verdict(
            CriticDecision.DOWNGRADE,
            downgraded_severity=Severity.LOW,
            downgraded_confidence=Confidence.LOW,
        ),
        corroborated_by_static=False,
    )
    assert result.final_severity == Severity.LOW
    assert result.final_confidence == Confidence.LOW


def test_downgrade_with_only_severity_leaves_confidence_alone() -> None:
    result = aggregate(
        reviewer_confidence=Confidence.HIGH,
        reviewer_severity=Severity.CRITICAL,
        critic_verdict=_verdict(CriticDecision.DOWNGRADE, downgraded_severity=Severity.MEDIUM),
        corroborated_by_static=False,
    )
    assert result.final_severity == Severity.MEDIUM
    assert result.final_confidence == Confidence.HIGH


def test_reject_verdict_raises_instead_of_aggregating() -> None:
    """A rejected proposal must be suppressed by the caller before ever
    reaching aggregation -- see patchfrog.review.service."""

    with pytest.raises(ValueError):
        aggregate(
            reviewer_confidence=Confidence.HIGH,
            reviewer_severity=Severity.HIGH,
            critic_verdict=_verdict(CriticDecision.REJECT),
            corroborated_by_static=False,
        )


def test_static_corroboration_boosts_confidence_by_one_step() -> None:
    result = aggregate(
        reviewer_confidence=Confidence.LOW,
        reviewer_severity=Severity.MEDIUM,
        critic_verdict=None,
        corroborated_by_static=True,
    )
    assert result.final_confidence == Confidence.MEDIUM
    assert result.corroborated_by_static is True


def test_static_corroboration_never_exceeds_high() -> None:
    result = aggregate(
        reviewer_confidence=Confidence.HIGH,
        reviewer_severity=Severity.MEDIUM,
        critic_verdict=None,
        corroborated_by_static=True,
    )
    assert result.final_confidence == Confidence.HIGH


def test_static_corroboration_never_exceeds_critic_downgrade_ceiling() -> None:
    """Corroboration can raise confidence, but never past what the critic
    was willing to accept."""

    result = aggregate(
        reviewer_confidence=Confidence.HIGH,
        reviewer_severity=Severity.HIGH,
        critic_verdict=_verdict(CriticDecision.DOWNGRADE, downgraded_confidence=Confidence.LOW),
        corroborated_by_static=True,
    )
    assert result.final_confidence == Confidence.LOW


def test_meets_minimum() -> None:
    assert meets_minimum(Confidence.HIGH, minimum=Confidence.MEDIUM) is True
    assert meets_minimum(Confidence.MEDIUM, minimum=Confidence.MEDIUM) is True
    assert meets_minimum(Confidence.LOW, minimum=Confidence.MEDIUM) is False
