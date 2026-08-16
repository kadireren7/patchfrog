from __future__ import annotations

from pathlib import Path

from patchfrog.context.budgeting import ContextBudgeter
from patchfrog.context.dedup import ScoredCandidate
from patchfrog.context.domain import (
    ContextCandidate,
    ContextItemKind,
    ContextRelationship,
    ScoreComponent,
)
from patchfrog.repository.snapshot import RepositorySnapshot


def _snapshot(root: Path) -> RepositorySnapshot:
    return RepositorySnapshot(repository_full_name="test/repo", commit_sha="abc123", root_path=root, owns_root=False)


def _write_file(root: Path, name: str, num_lines: int) -> None:
    (root / name).write_text("\n".join(f"line {i}" for i in range(1, num_lines + 1)) + "\n")


def _scored(
    *,
    kind: ContextItemKind,
    relationship: ContextRelationship,
    score: float,
    file_path: str,
    start_line: int,
    end_line: int,
) -> ScoredCandidate:
    candidate = ContextCandidate(
        kind=kind,
        file_path=file_path,
        symbol_id=None,
        symbol_name="f",
        qualified_name="f",
        start_line=start_line,
        end_line=end_line,
        relationship=relationship,
        distance=0 if kind is ContextItemKind.TARGET_SYMBOL else 1,
        reason="test",
    )
    return ScoredCandidate(candidate=candidate, score=score, breakdown=(ScoreComponent("x", score),))


def test_target_is_always_included_even_under_tight_budget(tmp_path: Path) -> None:
    _write_file(tmp_path, "a.py", 50)
    target = _scored(
        kind=ContextItemKind.TARGET_SYMBOL, relationship=ContextRelationship.TARGET_SYMBOL,
        score=1.0, file_path="a.py", start_line=1, end_line=20,
    )
    budgeter = ContextBudgeter()

    result = budgeter.build(
        _snapshot(tmp_path), kept=(target,), max_tokens=50, max_lines=10, max_tokens_per_item=800,
        max_lines_per_item=120, target_reservation_fraction=0.35, max_items_per_relationship=3,
    )

    assert len(result.items) == 1
    assert result.items[0].kind is ContextItemKind.TARGET_SYMBOL


def test_budget_is_never_exceeded(tmp_path: Path) -> None:
    _write_file(tmp_path, "a.py", 500)
    candidates = tuple(
        _scored(
            kind=ContextItemKind.CALLER, relationship=ContextRelationship.DIRECT_CALLER,
            score=1.0 - i * 0.01, file_path="a.py", start_line=i * 10 + 1, end_line=i * 10 + 8,
        )
        for i in range(30)
    )
    budgeter = ContextBudgeter()

    result = budgeter.build(
        _snapshot(tmp_path), kept=candidates, max_tokens=200, max_lines=40, max_tokens_per_item=800,
        max_lines_per_item=120, target_reservation_fraction=0.35, max_items_per_relationship=30,
    )

    assert result.total_tokens <= 200
    assert result.total_lines <= 40


def test_large_item_is_trimmed_not_excluded(tmp_path: Path) -> None:
    _write_file(tmp_path, "a.py", 500)
    huge = _scored(
        kind=ContextItemKind.TARGET_SYMBOL, relationship=ContextRelationship.TARGET_SYMBOL,
        score=1.0, file_path="a.py", start_line=1, end_line=500,
    )
    budgeter = ContextBudgeter()

    result = budgeter.build(
        _snapshot(tmp_path), kept=(huge,), max_tokens=4000, max_lines=400, max_tokens_per_item=800,
        max_lines_per_item=120, target_reservation_fraction=0.35, max_items_per_relationship=3,
    )

    assert len(result.items) == 1
    assert result.items[0].truncated is True
    assert result.items[0].end_line - result.items[0].start_line + 1 <= 140  # 400 * 0.35 = 140


def test_large_high_ranked_item_does_not_starve_everything_else(tmp_path: Path) -> None:
    _write_file(tmp_path, "a.py", 500)
    huge = _scored(
        kind=ContextItemKind.TARGET_SYMBOL, relationship=ContextRelationship.TARGET_SYMBOL,
        score=1.0, file_path="a.py", start_line=1, end_line=500,
    )
    small = _scored(
        kind=ContextItemKind.CALLER, relationship=ContextRelationship.DIRECT_CALLER,
        score=0.5, file_path="a.py", start_line=200, end_line=205,
    )
    budgeter = ContextBudgeter()

    result = budgeter.build(
        _snapshot(tmp_path), kept=(huge, small), max_tokens=4000, max_lines=400, max_tokens_per_item=800,
        max_lines_per_item=120, target_reservation_fraction=0.35, max_items_per_relationship=3,
    )

    kinds = [item.kind for item in result.items]
    assert ContextItemKind.TARGET_SYMBOL in kinds
    assert ContextItemKind.CALLER in kinds


def test_diversity_cap_limits_items_per_relationship(tmp_path: Path) -> None:
    _write_file(tmp_path, "a.py", 500)
    callers = tuple(
        _scored(
            kind=ContextItemKind.CALLER, relationship=ContextRelationship.DIRECT_CALLER,
            score=1.0 - i * 0.01, file_path="a.py", start_line=i * 10 + 1, end_line=i * 10 + 5,
        )
        for i in range(10)
    )
    budgeter = ContextBudgeter()

    result = budgeter.build(
        _snapshot(tmp_path), kept=callers, max_tokens=4000, max_lines=400, max_tokens_per_item=800,
        max_lines_per_item=120, target_reservation_fraction=0.35, max_items_per_relationship=3,
    )

    assert len(result.items) == 3
    assert result.dropped_budget == 7


def test_missing_source_file_is_dropped_not_crashing(tmp_path: Path) -> None:
    missing = _scored(
        kind=ContextItemKind.CALLER, relationship=ContextRelationship.DIRECT_CALLER,
        score=0.5, file_path="does_not_exist.py", start_line=1, end_line=5,
    )
    budgeter = ContextBudgeter()

    result = budgeter.build(
        _snapshot(tmp_path), kept=(missing,), max_tokens=4000, max_lines=400, max_tokens_per_item=800,
        max_lines_per_item=120, target_reservation_fraction=0.35, max_items_per_relationship=3,
    )

    assert len(result.items) == 0
    assert result.dropped_budget == 1


def test_very_small_budget_still_returns_something_for_target(tmp_path: Path) -> None:
    _write_file(tmp_path, "a.py", 50)
    target = _scored(
        kind=ContextItemKind.TARGET_SYMBOL, relationship=ContextRelationship.TARGET_SYMBOL,
        score=1.0, file_path="a.py", start_line=1, end_line=30,
    )
    budgeter = ContextBudgeter()

    result = budgeter.build(
        _snapshot(tmp_path), kept=(target,), max_tokens=20, max_lines=3, max_tokens_per_item=800,
        max_lines_per_item=120, target_reservation_fraction=0.35, max_items_per_relationship=3,
    )

    assert len(result.items) == 1
    assert result.items[0].truncated is True


def test_many_tiny_candidates_fill_budget_deterministically(tmp_path: Path) -> None:
    _write_file(tmp_path, "a.py", 500)
    candidates = tuple(
        _scored(
            kind=ContextItemKind.SIBLING_SYMBOL, relationship=ContextRelationship.SIBLING_SYMBOL,
            score=1.0 - i * 0.001, file_path="a.py", start_line=i * 5 + 1, end_line=i * 5 + 2,
        )
        for i in range(50)
    )
    budgeter = ContextBudgeter()

    result_a = budgeter.build(
        _snapshot(tmp_path), kept=candidates, max_tokens=100, max_lines=20, max_tokens_per_item=800,
        max_lines_per_item=120, target_reservation_fraction=0.35, max_items_per_relationship=50,
    )
    result_b = budgeter.build(
        _snapshot(tmp_path), kept=candidates, max_tokens=100, max_lines=20, max_tokens_per_item=800,
        max_lines_per_item=120, target_reservation_fraction=0.35, max_items_per_relationship=50,
    )

    assert [(i.start_line, i.end_line) for i in result_a.items] == [(i.start_line, i.end_line) for i in result_b.items]
    assert result_a.total_lines <= 20
