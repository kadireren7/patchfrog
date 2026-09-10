from __future__ import annotations

from patchfrog.fix_verification.domain import (
    ACTIVE_STATUSES,
    FIX_VERIFICATION_VERSION,
    TERMINAL_STATUSES,
    FixAttemptStatus,
    FixEvidenceDirection,
)


def test_fix_verification_version_is_one() -> None:
    assert FIX_VERIFICATION_VERSION == 1


def test_active_and_terminal_statuses_partition_all_statuses() -> None:
    all_statuses = set(FixAttemptStatus)
    assert all_statuses == ACTIVE_STATUSES | TERMINAL_STATUSES
    assert set() == ACTIVE_STATUSES & TERMINAL_STATUSES


def test_active_statuses_are_pending_and_verifying() -> None:
    assert {FixAttemptStatus.PENDING, FixAttemptStatus.VERIFYING} == ACTIVE_STATUSES


def test_terminal_statuses_cover_every_semantic_outcome() -> None:
    assert {
        FixAttemptStatus.FIXED,
        FixAttemptStatus.STILL_PRESENT,
        FixAttemptStatus.INCONCLUSIVE,
        FixAttemptStatus.STALE,
        FixAttemptStatus.ERROR,
    } == TERMINAL_STATUSES


def test_fix_evidence_direction_has_exactly_five_members() -> None:
    """Security correction (two rounds): weak signals in either direction
    (SUPPORTS_PRESENT/SUPPORTS_RESOLVED) must be distinct types from
    strong ones (CONFIRMS_PRESENT/PROVES_RESOLVED) -- never a bare bool a
    caller could accidentally treat as decisive."""

    assert {d.value for d in FixEvidenceDirection} == {
        "confirms_present", "supports_present", "supports_resolved", "proves_resolved", "no_signal",
    }
