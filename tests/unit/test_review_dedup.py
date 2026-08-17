from __future__ import annotations

import uuid

from patchfrog.analysis.domain import Confidence, FindingCategory, Severity
from patchfrog.review.dedup import deduplicate
from patchfrog.review.domain import (
    AIReviewFinding,
    FinalAIFinding,
    ReviewCandidate,
    ReviewCandidateReason,
)

_CANDIDATE = ReviewCandidate(
    file_path="src/billing.py",
    symbol_id=None,
    symbol_name="can_withdraw",
    qualified_name="src.billing.can_withdraw",
    start_line=1,
    end_line=3,
    changed_lines=(2,),
    static_finding_ids=(),
    reason=ReviewCandidateReason.CHANGED_SYMBOL,
)


def _final(
    *,
    file_path: str = "src/billing.py",
    start_line: int = 1,
    end_line: int = 3,
    category: FindingCategory = FindingCategory.CORRECTNESS,
    severity: Severity = Severity.HIGH,
    confidence: Confidence = Confidence.HIGH,
    title: str = "bug",
) -> FinalAIFinding:
    finding = AIReviewFinding(
        title=title,
        message="msg",
        category=category,
        severity=severity,
        confidence=confidence,
        file_path=file_path,
        start_line=start_line,
        end_line=end_line,
        evidence=(),
        reasoning_summary="x",
    )
    return FinalAIFinding(
        proposal_id=uuid.uuid4(),
        candidate_id=uuid.uuid4(),
        candidate=_CANDIDATE,
        finding=finding,
        critic_verdict=None,
        final_severity=severity,
        final_confidence=confidence,
        corroborated_by_static=False,
        static_finding_ids=(),
    )


def test_single_finding_is_kept() -> None:
    result = deduplicate((_final(),))
    assert len(result.kept) == 1
    assert result.suppressed == ()


def test_overlapping_same_category_findings_are_deduplicated() -> None:
    a = _final(start_line=1, end_line=5, severity=Severity.HIGH, title="a")
    b = _final(start_line=3, end_line=8, severity=Severity.LOW, title="b")
    result = deduplicate((a, b))
    assert len(result.kept) == 1
    assert len(result.suppressed) == 1
    assert result.kept[0].final_severity == Severity.HIGH  # higher severity wins


def test_non_overlapping_findings_are_both_kept() -> None:
    a = _final(start_line=1, end_line=3)
    b = _final(start_line=50, end_line=55)
    result = deduplicate((a, b))
    assert len(result.kept) == 2


def test_different_category_overlapping_findings_are_both_kept() -> None:
    a = _final(start_line=1, end_line=5, category=FindingCategory.CORRECTNESS)
    b = _final(start_line=1, end_line=5, category=FindingCategory.SECURITY)
    result = deduplicate((a, b))
    assert len(result.kept) == 2


def test_different_file_overlapping_line_numbers_are_both_kept() -> None:
    a = _final(file_path="src/a.py", start_line=1, end_line=5)
    b = _final(file_path="src/b.py", start_line=1, end_line=5)
    result = deduplicate((a, b))
    assert len(result.kept) == 2


def test_dedup_is_deterministic_regardless_of_input_order() -> None:
    a = _final(start_line=1, end_line=5, severity=Severity.HIGH, confidence=Confidence.HIGH, title="a")
    b = _final(start_line=2, end_line=6, severity=Severity.HIGH, confidence=Confidence.HIGH, title="b")
    forward = deduplicate((a, b))
    backward = deduplicate((b, a))
    assert forward.kept[0].finding.title == backward.kept[0].finding.title == "a"
