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


def test_no_test_surface_soundness_invariant() -> None:
    """Explicit statement of the soundness property audited in
    ``validation/test_intelligence/latest-summary.md`` section 9:
    "absence of a TEST_NOT_UPDATED companion really means 'no
    discoverable related test surface', not 'J skipped for some
    unrelated reason'." Both packages share the identical eligibility
    precondition (a changed candidate with a resolved ``symbol_id``,
    see ``patchfrog.change_intelligence.companions._test_staleness``
    and ``patchfrog.change_intelligence.service``'s per-unit call to
    ``derive_expected_companions`` with that same unit's own
    ``changed_candidates``) -- so whenever this milestone considers a
    file eligible, J's own companion derivation would have used the
    exact same precondition to look for its test files. A discovered
    related test -- whether OBSERVED (touched) or MISSING (not
    touched) -- always suppresses ``NO_TEST_SURFACE_FOUND``; only a
    truly undiscovered test surface (no companion at all) may emit
    it."""

    unit = ChangeUnit(
        id="u1", title="new behavior", change_kind=ChangeKind.BEHAVIOR,
        changed_candidates=(_candidate(file_path="service.py", qualified_name="process_payment"),),
    )

    observed = _companion(source_file_path="service.py", status=CompanionStatus.OBSERVED)
    assert derive_no_test_surface_expectations(change_units=(unit,), expected_companions=(observed,)) == ()

    missing = _companion(source_file_path="service.py", status=CompanionStatus.MISSING)
    assert derive_no_test_surface_expectations(change_units=(unit,), expected_companions=(missing,)) == ()

    truly_undiscovered = derive_no_test_surface_expectations(change_units=(unit,), expected_companions=())
    assert len(truly_undiscovered) == 1
    assert truly_undiscovered[0].reason_code is TestExpectationReasonCode.NO_TEST_SURFACE_FOUND


# ---- derive_weakened_test_expectations ----
#
# Anchored to a real, same-PR production change: only ever eligible for
# a test file that a real ExpectedCompanionChange
# (reason_code=TEST_NOT_UPDATED, status=OBSERVED) already confirms is
# linked to a changed production file and was itself genuinely touched.
# A companion is the *only* correlation mechanism -- never
# ChangeUnit.changed_candidates, never a filename-similarity guess.


def test_weakened_flagged_when_anchored_to_observed_companion() -> None:
    companion = _companion(source_file_path="service.py", status=CompanionStatus.OBSERVED)
    diff_file = _diff_file(
        path="test_service.py",
        deleted=["assert result.status == 'ok'", "assert result.amount == 100"],
        added=["# simplified test"],
    )
    expectations = derive_weakened_test_expectations(expected_companions=(companion,), diff_files=(diff_file,))
    assert len(expectations) == 1
    assert expectations[0].reason_code is TestExpectationReasonCode.TEST_TOUCHED_BUT_WEAKENED
    assert expectations[0].change_unit_id == "u1"
    assert "2 removed" in expectations[0].evidence.bounded_text


def test_weakened_never_flagged_without_a_companion_test_only_pr() -> None:
    """The mandatory test-only negative case: a PR touching only
    test_service.py, with no production change at all, produces zero
    ExpectedCompanionChange objects (companions are only ever derived
    from a changed *production* candidate's own test-file lookup -- see
    patchfrog.change_intelligence.companions._test_staleness), so this
    signal structurally cannot fire."""

    diff_file = _diff_file(
        path="test_service.py",
        deleted=["assert result.status == 'ok'", "assert result.amount == 100"],
        added=["# simplified test"],
    )
    assert derive_weakened_test_expectations(expected_companions=(), diff_files=(diff_file,)) == ()


def test_weakened_still_flagged_when_companion_status_is_missing_but_diff_shows_a_real_touch() -> None:
    """Deliberately does not gate on companion.status: J's own
    OBSERVED/MISSING split is derived from candidate-generated
    all_changed_file_paths, which is added-lines-only and therefore
    reports MISSING for a test file whose only change is a pure
    deletion -- even though it really was touched. Real diff membership
    is the authoritative "touched" signal here, not J's own status."""

    companion = _companion(source_file_path="service.py", status=CompanionStatus.MISSING)
    diff_file = _diff_file(
        path="test_service.py",
        deleted=["assert result.status == 'ok'"],
        added=[],
    )
    expectations = derive_weakened_test_expectations(expected_companions=(companion,), diff_files=(diff_file,))
    assert len(expectations) == 1
    assert expectations[0].reason_code is TestExpectationReasonCode.TEST_TOUCHED_BUT_WEAKENED


def test_weakened_never_flagged_when_companion_file_absent_from_diff_at_all() -> None:
    """The real "never touched" case: no diff entry exists for the
    companion's expected_file_path at all -- genuinely untouched,
    regardless of companion.status."""

    companion = _companion(source_file_path="service.py", status=CompanionStatus.MISSING)
    assert derive_weakened_test_expectations(expected_companions=(companion,), diff_files=()) == ()


def test_not_weakened_when_assertions_unchanged() -> None:
    companion = _companion(source_file_path="service.py", status=CompanionStatus.OBSERVED)
    diff_file = _diff_file(
        path="test_service.py",
        deleted=["import old_mock"],
        added=["import new_mock"],
    )
    assert derive_weakened_test_expectations(expected_companions=(companion,), diff_files=(diff_file,)) == ()


def test_not_weakened_when_assertions_strengthened() -> None:
    companion = _companion(source_file_path="service.py", status=CompanionStatus.OBSERVED)
    diff_file = _diff_file(
        path="test_service.py",
        deleted=["assert result.status == 'ok'"],
        added=["assert result.status == 'ok'", "assert result.amount == 100"],
    )
    assert derive_weakened_test_expectations(expected_companions=(companion,), diff_files=(diff_file,)) == ()


def test_weakened_flagged_when_skip_marker_added() -> None:
    companion = _companion(source_file_path="service.py", status=CompanionStatus.OBSERVED)
    diff_file = _diff_file(
        path="test_service.py",
        deleted=["def test_process_payment():"],
        added=["@pytest.mark.skip(reason='flaky')", "def test_process_payment():"],
    )
    expectations = derive_weakened_test_expectations(expected_companions=(companion,), diff_files=(diff_file,))
    assert len(expectations) == 1
    assert "skip/xfail" in expectations[0].evidence.bounded_text


def test_not_weakened_when_skip_marker_removed() -> None:
    """Un-skipping a test is strengthening, never flagged."""

    companion = _companion(source_file_path="service.py", status=CompanionStatus.OBSERVED)
    diff_file = _diff_file(
        path="test_service.py",
        deleted=["@pytest.mark.skip(reason='flaky')", "def test_process_payment():"],
        added=["def test_process_payment():"],
    )
    assert derive_weakened_test_expectations(expected_companions=(companion,), diff_files=(diff_file,)) == ()


def test_weakened_never_checked_for_a_non_companion_file() -> None:
    """A real weakening exists in the diff, but no companion names this
    file at all -- never guessed via filename similarity."""

    companion = _companion(source_file_path="service.py", status=CompanionStatus.OBSERVED)
    diff_file = _diff_file(path="test_unrelated.py", deleted=["assert x"], added=[])
    assert derive_weakened_test_expectations(expected_companions=(companion,), diff_files=(diff_file,)) == ()


def test_weakened_never_checked_when_file_absent_from_diff() -> None:
    companion = _companion(source_file_path="service.py", status=CompanionStatus.OBSERVED)
    assert derive_weakened_test_expectations(expected_companions=(companion,), diff_files=()) == ()


def test_weakened_deduplicates_repeated_companions_for_same_test_file() -> None:
    """Two production candidates in the same unit can both link to the
    same test file (two OBSERVED companions, same expected_file_path) --
    only one expectation, never a duplicate."""

    companions = (
        _companion(source_file_path="service.py", status=CompanionStatus.OBSERVED),
        ExpectedCompanionChange(
            change_unit_id="u1", source_qualified_name="other_fn", source_file_path="other.py",
            expected_qualified_name="test_process_payment", expected_file_path="test_service.py",
            reason_code=CompanionReasonCode.TEST_NOT_UPDATED, reason="likely test",
            evidence="file_tests_file edge", status=CompanionStatus.OBSERVED,
        ),
    )
    diff_file = _diff_file(path="test_service.py", deleted=["assert x", "assert y"], added=[])
    expectations = derive_weakened_test_expectations(expected_companions=companions, diff_files=(diff_file,))
    assert len(expectations) == 1


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
