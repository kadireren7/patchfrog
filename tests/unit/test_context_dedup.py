from __future__ import annotations

from patchfrog.context.dedup import ScoredCandidate, deduplicate
from patchfrog.context.domain import (
    ContextCandidate,
    ContextItemKind,
    ContextRelationship,
    ScoreComponent,
)


def _scored(
    *,
    score: float,
    relationship: ContextRelationship = ContextRelationship.DIRECT_CALLER,
    file_path: str = "a.py",
    start_line: int = 10,
    end_line: int = 20,
    symbol_id: object = None,
) -> ScoredCandidate:
    candidate = ContextCandidate(
        kind=ContextItemKind.CALLER,
        file_path=file_path,
        symbol_id=symbol_id,  # type: ignore[arg-type]
        symbol_name="f",
        qualified_name="f",
        start_line=start_line,
        end_line=end_line,
        relationship=relationship,
        distance=1,
        reason="test",
    )
    return ScoredCandidate(candidate=candidate, score=score, breakdown=(ScoreComponent("x", score),))


def test_exact_duplicate_span_is_dropped() -> None:
    a = _scored(score=0.9, start_line=10, end_line=20)
    b = _scored(score=0.5, start_line=10, end_line=20)

    result = deduplicate([a, b])

    assert len(result.kept) == 1
    assert result.kept[0].score == 0.9
    assert result.dropped_duplicate == 1
    assert result.dropped_overlap == 0


def test_heavily_overlapping_ranges_keep_only_higher_scored() -> None:
    a = _scored(score=0.9, start_line=20, end_line=80)  # 61 lines
    b = _scored(score=0.5, start_line=40, end_line=100)  # overlap 20-80&40-100 = 41 lines / 61 = 67%

    result = deduplicate([a, b])

    assert len(result.kept) == 1
    assert result.kept[0].score == 0.9
    assert result.dropped_overlap == 1


def test_non_overlapping_ranges_both_kept() -> None:
    a = _scored(score=0.9, start_line=10, end_line=20)
    b = _scored(score=0.5, start_line=50, end_line=60)

    result = deduplicate([a, b])

    assert len(result.kept) == 2
    assert result.dropped_duplicate == 0
    assert result.dropped_overlap == 0


def test_different_files_never_suppress_each_other() -> None:
    a = _scored(score=0.9, file_path="a.py", start_line=10, end_line=100)
    b = _scored(score=0.5, file_path="b.py", start_line=10, end_line=100)

    result = deduplicate([a, b])

    assert len(result.kept) == 2


def test_slight_overlap_under_threshold_keeps_both() -> None:
    a = _scored(score=0.9, start_line=1, end_line=100)  # 100 lines
    b = _scored(score=0.5, start_line=95, end_line=105)  # overlap 6 lines / 11 = 55% (below 60% threshold, of smaller span)

    result = deduplicate([a, b])

    assert len(result.kept) == 2


def test_result_is_ordered_by_score_descending() -> None:
    low = _scored(score=0.2, start_line=1, end_line=5)
    high = _scored(score=0.9, start_line=50, end_line=55)
    mid = _scored(score=0.5, start_line=100, end_line=105)

    result = deduplicate([low, high, mid])

    assert [s.score for s in result.kept] == [0.9, 0.5, 0.2]


def test_parent_symbol_is_never_overlap_suppressed_by_its_own_containment() -> None:
    """Regression: a class's span always structurally contains everything
    nested inside it, including the target -- if that were treated as
    "redundant overlap" like any other pair of spans, PARENT_SYMBOL
    context could never survive dedup for any nested target, since the
    higher-scored, fully-contained target would always suppress it."""

    target_candidate = ContextCandidate(
        kind=ContextItemKind.TARGET_SYMBOL, file_path="a.py", symbol_id=None, symbol_name="m",
        qualified_name="C.m", start_line=27, end_line=29, relationship=ContextRelationship.TARGET_SYMBOL,
        distance=0, reason="test",
    )
    target = ScoredCandidate(candidate=target_candidate, score=1.0, breakdown=(ScoreComponent("x", 1.0),))

    parent_candidate = ContextCandidate(
        kind=ContextItemKind.PARENT_SYMBOL, file_path="a.py", symbol_id=None, symbol_name="C",
        qualified_name="C", start_line=20, end_line=29, relationship=ContextRelationship.PARENT_SYMBOL,
        distance=1, reason="test",
    )
    parent = ScoredCandidate(candidate=parent_candidate, score=0.45, breakdown=(ScoreComponent("x", 0.45),))

    result = deduplicate([target, parent])

    assert len(result.kept) == 2
    assert result.dropped_overlap == 0


def test_deduplication_is_order_independent() -> None:
    a = _scored(score=0.9, start_line=10, end_line=20)
    b = _scored(score=0.5, start_line=10, end_line=20)
    c = _scored(score=0.3, start_line=50, end_line=60)

    forward = deduplicate([a, b, c])
    backward = deduplicate([c, b, a])

    assert [s.score for s in forward.kept] == [s.score for s in backward.kept]
    assert forward.dropped_duplicate == backward.dropped_duplicate
