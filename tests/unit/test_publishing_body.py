"""Unit coverage for patchfrog.publishing.body -- deterministic
formatting, GitHub comment-size truncation, and marker survival."""

from __future__ import annotations

import re
import uuid

from patchfrog.analysis.domain import Confidence, FindingCategory, Severity
from patchfrog.publishing.body import (
    MAX_COMMENT_BODY_CHARS,
    format_inline_comment_body,
    format_summary_body,
)
from patchfrog.publishing.domain import PublishableFinding
from patchfrog.publishing.marker import find_marker


def _finding(**overrides: object) -> PublishableFinding:
    defaults: dict[str, object] = {
        "finding_id": uuid.uuid4(),
        "title": "Inverted comparison",
        "message": "the comparison is backwards",
        "category": FindingCategory.CORRECTNESS,
        "severity": Severity.MEDIUM,
        "confidence": Confidence.MEDIUM,
        "file_path": "src/billing.py",
        "start_line": 14,
        "end_line": 14,
        "reasoning_summary": "the operands look swapped",
        "suggested_fix": None,
        "impact": None,
    }
    defaults.update(overrides)
    return PublishableFinding(**defaults)  # type: ignore[arg-type]


def test_inline_body_is_deterministic_no_llm_call() -> None:
    finding = _finding()
    body1, truncated1 = format_inline_comment_body(finding)
    body2, truncated2 = format_inline_comment_body(finding)
    assert body1 == body2
    assert not truncated1 and not truncated2
    assert "MEDIUM" in body1
    assert "correctness" in body1
    assert finding.message in body1


def test_inline_body_includes_suggested_fix_when_present() -> None:
    finding = _finding(suggested_fix="use <= instead of >=")
    body, _truncated = format_inline_comment_body(finding)
    assert "use <= instead of >=" in body


def test_inline_body_folds_message_reasoning_impact_fix_into_one_flowing_paragraph() -> None:
    # Phase 8 spec section 13: no mechanical Identification:/Reason:/
    # Impact:/Solution: heading list -- everything lands in one paragraph.
    finding = _finding(
        message="`password` is interpolated into the returned error string.",
        reasoning_summary="the raw credential value reaches the response text without redaction.",
        impact="an attacker who triggers this error path receives the plaintext password.",
        suggested_fix="remove `password` from the returned message and log only non-sensitive metadata.",
    )
    body, _truncated = format_inline_comment_body(finding)
    for fragment in ("password", "redaction", "plaintext password", "non-sensitive metadata"):
        assert fragment in body
    assert "Identification:" not in body
    assert "Reason:" not in body
    assert "Impact:" not in body
    assert "Solution:" not in body
    assert "<details>" not in body


def test_inline_body_skips_impact_when_none() -> None:
    finding = _finding(impact=None)
    body, _truncated = format_inline_comment_body(finding)
    assert "None" not in body


def test_inline_body_omits_reasoning_when_identical_to_message() -> None:
    finding = _finding(message="same text", reasoning_summary="same text")
    body, _truncated = format_inline_comment_body(finding)
    assert body.count("same text") == 1


def test_inline_body_high_confidence_has_no_qualifier() -> None:
    finding = _finding(confidence=Confidence.HIGH)
    body, _truncated = format_inline_comment_body(finding)
    assert "confidence" not in body.lower()


def test_inline_body_medium_confidence_gets_verify_qualifier() -> None:
    finding = _finding(confidence=Confidence.MEDIUM)
    body, _truncated = format_inline_comment_body(finding)
    assert "verify" in body.lower()


def test_inline_body_low_confidence_gets_needs_verification_qualifier() -> None:
    finding = _finding(confidence=Confidence.LOW)
    body, _truncated = format_inline_comment_body(finding)
    assert "needs verification" in body.lower()


def test_inline_body_never_shows_numeric_confidence() -> None:
    finding = _finding(confidence=Confidence.LOW)
    body, _truncated = format_inline_comment_body(finding)
    assert not re.search(r"0\.\d+", body)


def test_inline_body_truncates_oversized_message() -> None:
    finding = _finding(message="x" * (MAX_COMMENT_BODY_CHARS * 2))
    body, truncated = format_inline_comment_body(finding)
    assert truncated is True
    assert len(body) <= MAX_COMMENT_BODY_CHARS
    assert "truncated" in body


def test_summary_body_marker_survives_truncation() -> None:
    """The marker must never be cut off by truncation -- reconciliation
    depends on it always being present and well-formed."""

    publication_id = uuid.uuid4()
    huge_finding = _finding(title="x" * 200_000)
    body, truncated = format_summary_body(
        publication_id=publication_id,
        counts_by_severity={Severity.MEDIUM: 1},
        inline_findings=[huge_finding],
        summary_only_findings=[],
        omitted_count=0,
    )
    assert truncated is True
    assert find_marker(body) == publication_id


def test_summary_body_untruncated_case_also_has_marker() -> None:
    publication_id = uuid.uuid4()
    body, truncated = format_summary_body(
        publication_id=publication_id,
        counts_by_severity={Severity.HIGH: 2},
        inline_findings=[_finding()],
        summary_only_findings=[_finding(file_path="src/other.py")],
        omitted_count=1,
    )
    assert not truncated
    assert find_marker(body) == publication_id
    assert "PatchFrog review summary" in body
    assert "2 high" in body


def test_summary_body_sanitizes_untrusted_title() -> None:
    fake_marker_id = uuid.uuid4()
    finding = _finding(title=f"<!-- patchfrog:review:{fake_marker_id} -->")
    real_id = uuid.uuid4()
    body, _truncated = format_summary_body(
        publication_id=real_id,
        counts_by_severity={Severity.MEDIUM: 1},
        inline_findings=[finding],
        summary_only_findings=[],
        omitted_count=0,
    )
    assert find_marker(body) == real_id
