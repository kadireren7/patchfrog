"""Unit coverage for patchfrog.publishing.body -- deterministic
formatting, GitHub comment-size truncation, and marker survival."""

from __future__ import annotations

import re
import uuid

from patchfrog.analysis.domain import Confidence, FindingCategory, Severity
from patchfrog.publishing.body import (
    FROG_MARKER,
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
    assert "🐸 PatchFrog review" in body
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


# -- Branding & Review Presentation Refinement -------------------------------


def test_inline_body_has_frog_marker_exactly_once_by_default() -> None:
    finding = _finding()
    body, _truncated = format_inline_comment_body(finding)
    assert body.count(FROG_MARKER) == 1
    assert body.startswith(FROG_MARKER)


def test_inline_body_frog_marker_can_be_disabled() -> None:
    finding = _finding()
    body, _truncated = format_inline_comment_body(finding, frog_marker=False)
    assert FROG_MARKER not in body
    assert body.startswith("**")


def test_inline_body_shows_severity_and_category_header() -> None:
    finding = _finding(severity=Severity.HIGH, category=FindingCategory.SECURITY)
    body, _truncated = format_inline_comment_body(finding)
    assert f"{FROG_MARKER} **HIGH · security**" in body


def test_inline_body_omits_category_when_unknown() -> None:
    finding = _finding(category=FindingCategory.UNKNOWN)
    body, _truncated = format_inline_comment_body(finding)
    assert f"{FROG_MARKER} **MEDIUM**" in body
    assert "unknown" not in body.lower()


def test_inline_body_low_severity_renders() -> None:
    finding = _finding(severity=Severity.LOW)
    body, _truncated = format_inline_comment_body(finding)
    assert "LOW" in body


def test_inline_body_never_says_ai_or_llm_finding() -> None:
    finding = _finding()
    body, _truncated = format_inline_comment_body(finding)
    assert "ai finding" not in body.lower()
    assert "llm finding" not in body.lower()
    assert "powered by" not in body.lower()


def test_inline_body_never_repeats_patchfrog_name() -> None:
    """GitHub already shows `patchfrog[bot]` as the comment's author --
    the inline body itself must never also say "PatchFrog"."""

    finding = _finding()
    body, _truncated = format_inline_comment_body(finding)
    assert "patchfrog" not in body.lower()


def test_summary_body_heading_is_frog_patchfrog_review() -> None:
    body, _truncated = format_summary_body(
        publication_id=uuid.uuid4(),
        counts_by_severity={Severity.HIGH: 1, Severity.MEDIUM: 2},
        inline_findings=[_finding()],
        summary_only_findings=[],
        omitted_count=0,
    )
    assert body.startswith(f"## {FROG_MARKER} PatchFrog review\n")
    assert body.count("PatchFrog") == 1
    assert body.count(FROG_MARKER) == 1


def test_summary_body_frog_marker_can_be_disabled() -> None:
    body, _truncated = format_summary_body(
        publication_id=uuid.uuid4(),
        counts_by_severity={Severity.HIGH: 1},
        inline_findings=[_finding()],
        summary_only_findings=[],
        omitted_count=0,
        frog_marker=False,
    )
    assert FROG_MARKER not in body
    assert body.startswith("## PatchFrog review\n")


def test_summary_body_findings_line_uses_middot_separator() -> None:
    body, _truncated = format_summary_body(
        publication_id=uuid.uuid4(),
        counts_by_severity={Severity.HIGH: 1, Severity.MEDIUM: 2},
        inline_findings=[_finding()],
        summary_only_findings=[],
        omitted_count=0,
    )
    assert "**Findings:** 1 high · 2 medium" in body


def test_summary_body_omits_zero_omitted_count() -> None:
    body, _truncated = format_summary_body(
        publication_id=uuid.uuid4(),
        counts_by_severity={Severity.HIGH: 1},
        inline_findings=[_finding()],
        summary_only_findings=[],
        omitted_count=0,
    )
    assert "Omitted" not in body


def test_summary_body_shows_nonzero_omitted_count() -> None:
    body, _truncated = format_summary_body(
        publication_id=uuid.uuid4(),
        counts_by_severity={Severity.HIGH: 1},
        inline_findings=[_finding()],
        summary_only_findings=[],
        omitted_count=3,
    )
    assert "**Omitted:** 3" in body


def test_summary_body_never_has_marketing_copy() -> None:
    body, _truncated = format_summary_body(
        publication_id=uuid.uuid4(),
        counts_by_severity={Severity.HIGH: 1},
        inline_findings=[_finding()],
        summary_only_findings=[],
        omitted_count=0,
    )
    for phrase in ("ai-powered", "powered by ai", "code review assistant", "cutting-edge"):
        assert phrase not in body.lower()


def test_confidence_wording_unaffected_by_branding() -> None:
    """The Security Review Quality confidence-qualifier behavior must be
    completely unchanged by the branding refinement."""

    finding = _finding(confidence=Confidence.LOW)
    body, _truncated = format_inline_comment_body(finding)
    assert "needs verification" in body.lower()
    assert not re.search(r"0\.\d+", body)
    assert "confidence:" not in body.lower()
