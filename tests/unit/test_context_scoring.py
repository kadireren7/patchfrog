from __future__ import annotations

from patchfrog.analysis.domain import FindingCategory
from patchfrog.context.domain import ContextCandidate, ContextItemKind, ContextRelationship
from patchfrog.context.scoring import relationship_priority_index, score_candidate

_DEFAULT_KWARGS: dict[str, object] = {
    "file_path": "a.py",
    "symbol_id": None,
    "symbol_name": "f",
    "qualified_name": "f",
    "start_line": 1,
    "end_line": 5,
    "reason": "test",
}


def _candidate(**overrides: object) -> ContextCandidate:
    kwargs = {**_DEFAULT_KWARGS, **overrides}
    return ContextCandidate(kind=ContextItemKind.CALLER, **kwargs)  # type: ignore[arg-type]


def test_target_symbol_scores_highest() -> None:
    target = _candidate(relationship=ContextRelationship.TARGET_SYMBOL, distance=0)
    caller = _candidate(relationship=ContextRelationship.DIRECT_CALLER, distance=1)

    target_score, _ = score_candidate(target, target_file_path="a.py", finding_category=None)
    caller_score, _ = score_candidate(caller, target_file_path="a.py", finding_category=None)

    assert target_score > caller_score


def test_direct_caller_beats_transitive_caller() -> None:
    direct, _ = score_candidate(
        _candidate(relationship=ContextRelationship.DIRECT_CALLER, distance=1),
        target_file_path="a.py",
        finding_category=None,
    )
    transitive, _ = score_candidate(
        _candidate(relationship=ContextRelationship.TRANSITIVE_CALLER, distance=2),
        target_file_path="a.py",
        finding_category=None,
    )
    assert direct > transitive


def test_related_test_outranks_unrelated_sibling() -> None:
    test_score, _ = score_candidate(
        _candidate(relationship=ContextRelationship.TESTS_TARGET_FILE, distance=1),
        target_file_path="a.py",
        finding_category=None,
    )
    sibling_score, _ = score_candidate(
        _candidate(relationship=ContextRelationship.SIBLING_SYMBOL, distance=1),
        target_file_path="a.py",
        finding_category=None,
    )
    assert test_score > sibling_score


def test_same_file_bonus_applies() -> None:
    same_file, _ = score_candidate(
        _candidate(relationship=ContextRelationship.SIBLING_SYMBOL, distance=1, file_path="a.py"),
        target_file_path="a.py",
        finding_category=None,
    )
    other_file, _ = score_candidate(
        _candidate(relationship=ContextRelationship.SIBLING_SYMBOL, distance=1, file_path="b.py"),
        target_file_path="a.py",
        finding_category=None,
    )
    assert same_file > other_file


def test_changed_line_boost_applies() -> None:
    changed, _ = score_candidate(
        _candidate(relationship=ContextRelationship.DIRECT_CALLER, distance=1, is_on_changed_line=True),
        target_file_path="z.py",
        finding_category=None,
    )
    unchanged, _ = score_candidate(
        _candidate(relationship=ContextRelationship.DIRECT_CALLER, distance=1, is_on_changed_line=False),
        target_file_path="z.py",
        finding_category=None,
    )
    assert changed > unchanged


def test_distance_penalty_reduces_score() -> None:
    depth_1, _ = score_candidate(
        _candidate(relationship=ContextRelationship.DIRECT_CALLER, distance=1),
        target_file_path="z.py",
        finding_category=None,
    )
    depth_2, _ = score_candidate(
        _candidate(relationship=ContextRelationship.TRANSITIVE_CALLER, distance=2),
        target_file_path="z.py",
        finding_category=None,
    )
    assert depth_1 > depth_2


def test_finding_category_bonus_only_applies_to_preferred_relationships() -> None:
    caller = _candidate(relationship=ContextRelationship.DIRECT_CALLER, distance=1)
    sibling = _candidate(relationship=ContextRelationship.SIBLING_SYMBOL, distance=1)

    caller_with_finding, caller_breakdown = score_candidate(
        caller, target_file_path="z.py", finding_category=FindingCategory.MEMORY_SAFETY
    )
    caller_without_finding, _ = score_candidate(caller, target_file_path="z.py", finding_category=None)
    assert caller_with_finding > caller_without_finding
    assert any(c.label.startswith("finding_category:") for c in caller_breakdown)

    sibling_with_finding, _ = score_candidate(
        sibling, target_file_path="z.py", finding_category=FindingCategory.MEMORY_SAFETY
    )
    sibling_without_finding, _ = score_candidate(sibling, target_file_path="z.py", finding_category=None)
    assert sibling_with_finding == sibling_without_finding


def test_score_breakdown_sums_to_score() -> None:
    candidate = _candidate(
        relationship=ContextRelationship.DIRECT_CALLER, distance=1, is_on_changed_line=True, file_path="z.py"
    )
    score, breakdown = score_candidate(candidate, target_file_path="z.py", finding_category=None)
    assert score == sum(c.value for c in breakdown)


def test_relationship_priority_is_a_total_deterministic_order() -> None:
    indices = [relationship_priority_index(r) for r in ContextRelationship]
    assert len(indices) == len(set(indices))
