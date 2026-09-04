"""Unit tests for :mod:`patchfrog.test_intelligence.expectations` -- the
two genuinely new structural signals (spec section 1's audit). Pure/
synchronous, no database, no LLM -- every case is a hand-built
:class:`~patchfrog.change_intelligence.domain.ChangeUnit` and/or
:class:`~patchfrog.diff.models.DiffFile`.
"""

from __future__ import annotations

import uuid

from patchfrog.change_intelligence.domain import (
    ChangeKind,
    ChangeUnit,
    CompanionReasonCode,
    CompanionStatus,
    ExpectedCompanionChange,
)
from patchfrog.diff.models import DiffFile, DiffHunk, DiffLine, DiffLineType
from patchfrog.review.domain import ReviewCandidate, ReviewCandidateReason
from patchfrog.test_intelligence.domain import (
    MAX_TEST_GAPS_PER_UNIT,
    TestExpectationReasonCode,
    TestSurface,
)
from patchfrog.test_intelligence.expectations import (
    derive_gaps,
    derive_no_test_surface_expectations,
    derive_test_surfaces,
    derive_weakened_test_expectations,
)


def _candidate(*, file_path: str, qualified_name: str | None, symbol_id: uuid.UUID | None = None) -> ReviewCandidate:
    return ReviewCandidate(
        file_path=file_path,
        symbol_id=symbol_id if symbol_id is not None else (uuid.uuid4() if qualified_name else None),
        symbol_name=qualified_name.rsplit(".", 1)[-1] if qualified_name else None,
        qualified_name=qualified_name, start_line=1, end_line=5, changed_lines=(1,),
        static_finding_ids=(), reason=ReviewCandidateReason.CHANGED_SYMBOL,
    )


def _companion(
    *, source_file_path: str, status: CompanionStatus, reason_code: CompanionReasonCode = CompanionReasonCode.TEST_NOT_UPDATED
) -> ExpectedCompanionChange:
    return ExpectedCompanionChange(
        change_unit_id="u1", source_qualified_name="process_payment", source_file_path=source_file_path,
        expected_qualified_name="test_process_payment", expected_file_path="test_service.py",
        reason_code=reason_code, reason="likely test", evidence="file_tests_file edge", status=status,
    )


def _diff_file(*, path: str, added: list[str], deleted: list[str]) -> DiffFile:
    lines = [
        DiffLine(line_type=DiffLineType.DELETION, old_line_number=i + 1, new_line_number=None, content=content)
        for i, content in enumerate(deleted)
    ] + [
        DiffLine(line_type=DiffLineType.ADDITION, old_line_number=None, new_line_number=i + 1, content=content)
        for i, content in enumerate(added)
    ]
    hunk = DiffHunk(old_start=1, old_lines=len(deleted), new_start=1, new_lines=len(added), section_heading=None, lines=tuple(lines))
    return DiffFile(path=path, hunks=(hunk,))


# ---- derive_test_surfaces ----


def test_test_surface_not_discovered_with_no_companion() -> None:
    surfaces = derive_test_surfaces(changed_file_paths=frozenset({"service.py"}), expected_companions=())
    assert surfaces["service.py"] == TestSurface(file_path="service.py")
    assert not surfaces["service.py"].discovered


def test_test_surface_discovered_regardless_of_companion_status() -> None:
    for status in (CompanionStatus.OBSERVED, CompanionStatus.MISSING):
        surfaces = derive_test_surfaces(
            changed_file_paths=frozenset({"service.py"}),
            expected_companions=(_companion(source_file_path="service.py", status=status),),
        )
        assert surfaces["service.py"].discovered
        assert surfaces["service.py"].known_test_file_paths == ("test_service.py",)


# ---- derive_no_test_surface_expectations ----


def test_no_test_surface_flagged_for_behavior_unit_with_no_companion() -> None:
    unit = ChangeUnit(
        id="u1", title="new behavior", change_kind=ChangeKind.BEHAVIOR,
        changed_candidates=(_candidate(file_path="service.py", qualified_name="process_payment"),),
    )
    expectations = derive_no_test_surface_expectations(change_units=(unit,), expected_companions=())
    assert len(expectations) == 1
    assert expectations[0].reason_code is TestExpectationReasonCode.NO_TEST_SURFACE_FOUND
    assert expectations[0].status is CompanionStatus.MISSING
    assert expectations[0].source_file_path == "service.py"


def test_no_test_surface_suppressed_when_companion_found_missing() -> None:
    """Dedup: even a MISSING TEST_NOT_UPDATED companion means J already
    found a real test link -- never re-flagged here."""

    unit = ChangeUnit(
        id="u1", title="new behavior", change_kind=ChangeKind.BEHAVIOR,
        changed_candidates=(_candidate(file_path="service.py", qualified_name="process_payment"),),
    )
    companion = _companion(source_file_path="service.py", status=CompanionStatus.MISSING)
    expectations = derive_no_test_surface_expectations(change_units=(unit,), expected_companions=(companion,))
    assert expectations == ()


def test_no_test_surface_suppressed_when_companion_found_observed() -> None:
    unit = ChangeUnit(
        id="u1", title="new behavior", change_kind=ChangeKind.BEHAVIOR,
        changed_candidates=(_candidate(file_path="service.py", qualified_name="process_payment"),),
    )
    companion = _companion(source_file_path="service.py", status=CompanionStatus.OBSERVED)
    expectations = derive_no_test_surface_expectations(change_units=(unit,), expected_companions=(companion,))
    assert expectations == ()


def test_no_test_surface_never_fires_for_contract_kind_unit() -> None:
    unit = ChangeUnit(
        id="u1", title="contract change", change_kind=ChangeKind.CONTRACT,
        changed_candidates=(_candidate(file_path="service.py", qualified_name="process_payment"),),
    )
    assert derive_no_test_surface_expectations(change_units=(unit,), expected_companions=()) == ()


def test_no_test_surface_never_fires_for_mixed_unit() -> None:
    unit = ChangeUnit(
        id="u1", title="mixed change", change_kind=ChangeKind.MIXED,
        changed_candidates=(_candidate(file_path="service.py", qualified_name="process_payment"),),
    )
    assert derive_no_test_surface_expectations(change_units=(unit,), expected_companions=()) == ()


def test_no_test_surface_never_fires_for_unresolved_symbol() -> None:
    unit = ChangeUnit(
        id="u1", title="module region", change_kind=ChangeKind.BEHAVIOR,
        changed_candidates=(_candidate(file_path="service.py", qualified_name=None, symbol_id=None),),
    )
    assert derive_no_test_surface_expectations(change_units=(unit,), expected_companions=()) == ()


# ---- derive_weakened_test_expectations ----


def test_weakened_flagged_when_net_assertions_decrease() -> None:
    unit = ChangeUnit(
        id="u1", title="test change", change_kind=ChangeKind.TEST,
        changed_candidates=(_candidate(file_path="test_service.py", qualified_name="test_process_payment"),),
    )
    diff_file = _diff_file(
        path="test_service.py",
        deleted=["assert result.status == 'ok'", "assert result.amount == 100"],
        added=["# simplified test"],
    )
    expectations = derive_weakened_test_expectations(change_units=(unit,), diff_files=(diff_file,))
    assert len(expectations) == 1
    assert expectations[0].reason_code is TestExpectationReasonCode.TEST_TOUCHED_BUT_WEAKENED
    assert "2 removed" in expectations[0].evidence.bounded_text


def test_not_weakened_when_assertions_unchanged() -> None:
    unit = ChangeUnit(
        id="u1", title="test change", change_kind=ChangeKind.TEST,
        changed_candidates=(_candidate(file_path="test_service.py", qualified_name="test_process_payment"),),
    )
    diff_file = _diff_file(
        path="test_service.py",
        deleted=["import old_mock"],
        added=["import new_mock"],
    )
    assert derive_weakened_test_expectations(change_units=(unit,), diff_files=(diff_file,)) == ()


def test_not_weakened_when_assertions_strengthened() -> None:
    unit = ChangeUnit(
        id="u1", title="test change", change_kind=ChangeKind.TEST,
        changed_candidates=(_candidate(file_path="test_service.py", qualified_name="test_process_payment"),),
    )
    diff_file = _diff_file(
        path="test_service.py",
        deleted=["assert result.status == 'ok'"],
        added=["assert result.status == 'ok'", "assert result.amount == 100"],
    )
    assert derive_weakened_test_expectations(change_units=(unit,), diff_files=(diff_file,)) == ()


def test_weakened_flagged_when_skip_marker_added() -> None:
    unit = ChangeUnit(
        id="u1", title="test change", change_kind=ChangeKind.TEST,
        changed_candidates=(_candidate(file_path="test_service.py", qualified_name="test_process_payment"),),
    )
    diff_file = _diff_file(
        path="test_service.py",
        deleted=["def test_process_payment():"],
        added=["@pytest.mark.skip(reason='flaky')", "def test_process_payment():"],
    )
    expectations = derive_weakened_test_expectations(change_units=(unit,), diff_files=(diff_file,))
    assert len(expectations) == 1
    assert "skip/xfail" in expectations[0].evidence.bounded_text


def test_not_weakened_when_skip_marker_removed() -> None:
    """Un-skipping a test is strengthening, never flagged."""

    unit = ChangeUnit(
        id="u1", title="test change", change_kind=ChangeKind.TEST,
        changed_candidates=(_candidate(file_path="test_service.py", qualified_name="test_process_payment"),),
    )
    diff_file = _diff_file(
        path="test_service.py",
        deleted=["@pytest.mark.skip(reason='flaky')", "def test_process_payment():"],
        added=["def test_process_payment():"],
    )
    assert derive_weakened_test_expectations(change_units=(unit,), diff_files=(diff_file,)) == ()


def test_weakened_never_checked_for_non_test_file() -> None:
    unit = ChangeUnit(
        id="u1", title="behavior change", change_kind=ChangeKind.BEHAVIOR,
        changed_candidates=(_candidate(file_path="service.py", qualified_name="process_payment"),),
    )
    diff_file = _diff_file(path="service.py", deleted=["assert x"], added=[])
    assert derive_weakened_test_expectations(change_units=(unit,), diff_files=(diff_file,)) == ()


def test_weakened_never_checked_when_file_absent_from_diff() -> None:
    unit = ChangeUnit(
        id="u1", title="test change", change_kind=ChangeKind.TEST,
        changed_candidates=(_candidate(file_path="test_service.py", qualified_name="test_process_payment"),),
    )
    assert derive_weakened_test_expectations(change_units=(unit,), diff_files=()) == ()


# ---- derive_gaps ----


def test_gaps_bounded_per_unit() -> None:
    expectations = tuple(
        derive_no_test_surface_expectations(
            change_units=(
                ChangeUnit(
                    id="u1", title="new behavior", change_kind=ChangeKind.BEHAVIOR,
                    changed_candidates=tuple(
                        _candidate(file_path=f"s{i}.py", qualified_name=f"process_{i}")
                        for i in range(MAX_TEST_GAPS_PER_UNIT + 3)
                    ),
                ),
            ),
            expected_companions=(),
        )
    )
    gaps = derive_gaps(expectations)
    assert len(gaps) == MAX_TEST_GAPS_PER_UNIT
