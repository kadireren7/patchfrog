from __future__ import annotations

import uuid

from patchfrog.analysis.domain import Confidence, FindingCategory, Severity
from patchfrog.persistence.models.analysis import FindingModel, FindingStatus
from patchfrog.review.candidates import _attach_static_findings, _cluster_lines, _prioritize
from patchfrog.review.domain import ReviewCandidate, ReviewCandidateReason


def _candidate(**overrides: object) -> ReviewCandidate:
    kwargs: dict[str, object] = {
        "file_path": "src/a.py",
        "symbol_id": None,
        "symbol_name": "f",
        "qualified_name": "f",
        "start_line": 1,
        "end_line": 10,
        "changed_lines": (2,),
        "static_finding_ids": (),
        "reason": ReviewCandidateReason.CHANGED_SYMBOL,
    }
    kwargs.update(overrides)
    return ReviewCandidate(**kwargs)  # type: ignore[arg-type]


def _finding_model(*, file_path: str, start_line: int, end_line: int) -> FindingModel:
    return FindingModel(
        id=uuid.uuid4(),
        analysis_run_id=uuid.uuid4(),
        fingerprint="f",
        rule_id="r",
        category=FindingCategory.CORRECTNESS,
        title="t",
        message="m",
        severity=Severity.HIGH,
        confidence=Confidence.HIGH,
        file_path=file_path,
        start_line=start_line,
        end_line=end_line,
        start_column=0,
        end_column=0,
        source_analyzer="ruff",
        status=FindingStatus.ACTIVE,
    )


def test_cluster_lines_groups_nearby_lines() -> None:
    clusters = _cluster_lines([1, 2, 3, 50, 51], max_span=10)
    assert clusters == [[1, 2, 3], [50, 51]]


def test_cluster_lines_empty_input() -> None:
    assert _cluster_lines([], max_span=10) == []


def test_cluster_lines_single_line() -> None:
    assert _cluster_lines([5], max_span=10) == [[5]]


def test_cluster_lines_splits_on_span_not_just_gap() -> None:
    # Consecutive lines with no individual gap > max_span, but the total
    # span from the cluster's first line exceeds max_span -> must split.
    lines = list(range(1, 30))  # span 29, max_span 10
    clusters = _cluster_lines(lines, max_span=10)
    assert len(clusters) > 1
    assert all(c[-1] - c[0] <= 10 for c in clusters)


def test_attach_static_findings_matches_overlapping_range() -> None:
    finding = _finding_model(file_path="src/a.py", start_line=5, end_line=5)
    candidate = _candidate(file_path="src/a.py", start_line=1, end_line=10)
    result = _attach_static_findings([candidate], [finding])
    assert result[0].static_finding_ids == (finding.id,)


def test_attach_static_findings_ignores_different_file() -> None:
    finding = _finding_model(file_path="src/other.py", start_line=5, end_line=5)
    candidate = _candidate(file_path="src/a.py", start_line=1, end_line=10)
    result = _attach_static_findings([candidate], [finding])
    assert result[0].static_finding_ids == ()


def test_attach_static_findings_ignores_out_of_range() -> None:
    finding = _finding_model(file_path="src/a.py", start_line=500, end_line=500)
    candidate = _candidate(file_path="src/a.py", start_line=1, end_line=10)
    result = _attach_static_findings([candidate], [finding])
    assert result[0].static_finding_ids == ()


def test_attach_static_findings_no_findings_returns_input_unchanged() -> None:
    candidate = _candidate()
    result = _attach_static_findings([candidate], [])
    assert result == [candidate]


def test_prioritize_puts_static_corroborated_candidates_first() -> None:
    with_static = _candidate(file_path="z.py", static_finding_ids=(uuid.uuid4(),))
    without_static = _candidate(file_path="a.py")
    result = _prioritize([without_static, with_static])
    assert result[0] is with_static


def test_prioritize_breaks_ties_by_more_changed_lines() -> None:
    small = _candidate(file_path="a.py", changed_lines=(1,))
    large = _candidate(file_path="b.py", changed_lines=(1, 2, 3))
    result = _prioritize([small, large])
    assert result[0] is large


def test_prioritize_is_deterministic_by_file_path_and_line() -> None:
    a = _candidate(file_path="z.py", start_line=1)
    b = _candidate(file_path="a.py", start_line=1)
    result_1 = _prioritize([a, b])
    result_2 = _prioritize([b, a])
    assert [c.file_path for c in result_1] == [c.file_path for c in result_2] == ["a.py", "z.py"]
