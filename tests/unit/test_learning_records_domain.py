"""Pure domain tests for Milestone Y's durable learning records --
maturity classification and the repetition floor (spec tests Y2/Y3)."""

from __future__ import annotations

import pytest

from patchfrog.learning_records.domain import (
    ESTABLISHED_SUPPORT_THRESHOLD,
    MIN_SUPPORTING_EVENTS,
    LearningMaturity,
    classify_maturity,
)


def test_below_floor_raises_never_constructs_a_learning() -> None:
    with pytest.raises(ValueError, match="below MIN_SUPPORTING_EVENTS"):
        classify_maturity(MIN_SUPPORTING_EVENTS - 1)


def test_at_floor_is_candidate() -> None:
    assert classify_maturity(MIN_SUPPORTING_EVENTS) is LearningMaturity.CANDIDATE


def test_below_established_threshold_stays_candidate() -> None:
    assert classify_maturity(ESTABLISHED_SUPPORT_THRESHOLD - 1) is LearningMaturity.CANDIDATE


def test_at_established_threshold_is_established() -> None:
    assert classify_maturity(ESTABLISHED_SUPPORT_THRESHOLD) is LearningMaturity.ESTABLISHED


def test_well_above_threshold_is_established() -> None:
    assert classify_maturity(1000) is LearningMaturity.ESTABLISHED


def test_single_event_never_produces_a_learning_a_priori() -> None:
    """A single event is exactly MIN_SUPPORTING_EVENTS - 1 by definition
    (the floor is the smallest number that can mean "repeated")."""

    assert MIN_SUPPORTING_EVENTS == 2
    with pytest.raises(ValueError):
        classify_maturity(1)
