"""Unit tests for :mod:`patchfrog.executable_verification.eligibility` --
pure, deterministic derivation reusing
:func:`patchfrog.test_intelligence.expectations.derive_test_surfaces`
directly. No filename-similarity guessing, no database, no LLM."""

from __future__ import annotations

from patchfrog.change_intelligence.domain import (
    CompanionReasonCode,
    CompanionStatus,
    ExpectedCompanionChange,
)
from patchfrog.executable_verification.eligibility import determine_verification_target
from patchfrog.review.domain import ReviewCandidate, ReviewCandidateReason


def _candidate(*, file_path: str = "src/billing/capture.py", qualified_name: str | None = "capture_payment") -> ReviewCandidate:
    return ReviewCandidate(
        file_path=file_path, symbol_id=None, symbol_name=qualified_name.rsplit(".", 1)[-1] if qualified_name else None,
        qualified_name=qualified_name, start_line=1, end_line=10, changed_lines=(2,),
        static_finding_ids=(), reason=ReviewCandidateReason.CHANGED_SYMBOL,
    )


def _companion(
    *, source_file_path: str, expected_file_path: str, reason_code: CompanionReasonCode = CompanionReasonCode.TEST_NOT_UPDATED,
) -> ExpectedCompanionChange:
    return ExpectedCompanionChange(
        change_unit_id="unit-1", source_qualified_name="capture_payment", source_file_path=source_file_path,
        expected_qualified_name="test_capture_payment", expected_file_path=expected_file_path,
        reason_code=reason_code, reason="r", evidence="e", status=CompanionStatus.MISSING,
    )


def test_no_companions_no_target() -> None:
    assert determine_verification_target(candidate=_candidate(), expected_companions=()) is None


def test_real_test_not_updated_companion_gives_target() -> None:
    companion = _companion(source_file_path="src/billing/capture.py", expected_file_path="tests/test_capture.py")
    target = determine_verification_target(candidate=_candidate(), expected_companions=(companion,))
    assert target == "tests/test_capture.py"


def test_companion_for_a_different_file_never_matches() -> None:
    companion = _companion(source_file_path="src/other.py", expected_file_path="tests/test_other.py")
    assert determine_verification_target(candidate=_candidate(), expected_companions=(companion,)) is None


def test_non_test_not_updated_reason_code_ignored() -> None:
    companion = _companion(
        source_file_path="src/billing/capture.py", expected_file_path="tests/test_capture.py",
        reason_code=CompanionReasonCode.CALLER_NOT_UPDATED,
    )
    assert determine_verification_target(candidate=_candidate(), expected_companions=(companion,)) is None


def test_module_region_candidate_still_eligible() -> None:
    """The relationship is file-level, not symbol-level -- a
    module-region candidate (qualified_name=None) is still eligible."""

    companion = _companion(source_file_path="src/billing/capture.py", expected_file_path="tests/test_capture.py")
    candidate = _candidate(qualified_name=None)
    target = determine_verification_target(candidate=candidate, expected_companions=(companion,))
    assert target == "tests/test_capture.py"


def test_first_discovered_path_used_deterministically() -> None:
    companions = (
        _companion(source_file_path="src/billing/capture.py", expected_file_path="tests/test_capture_b.py"),
        _companion(source_file_path="src/billing/capture.py", expected_file_path="tests/test_capture_a.py"),
    )
    target = determine_verification_target(candidate=_candidate(), expected_companions=companions)
    # derive_test_surfaces sorts+dedups known_test_file_paths -- alphabetically first.
    assert target == "tests/test_capture_a.py"
