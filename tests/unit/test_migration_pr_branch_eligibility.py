"""M9.2 / M9.3: deterministic branch naming and publication eligibility."""

from __future__ import annotations

from patchfrog.migration_pr.branch import migration_branch_name
from patchfrog.migration_pr.domain import MigrationPREligibility, MigrationPRPolicy
from patchfrog.migration_pr.eligibility import determine_eligibility
from patchfrog.migration_verification.domain import VerificationOutcome


def test_branch_name_is_stable_for_same_identity() -> None:
    a = migration_branch_name(provider="acme-ai", change_fingerprint="abc123def456" + "0" * 52)
    b = migration_branch_name(provider="acme-ai", change_fingerprint="abc123def456" + "0" * 52)
    assert a == b
    assert a == "patchfrog/migrate/acme-ai/abc123def456"


def test_branch_name_differs_for_different_change_fingerprint() -> None:
    a = migration_branch_name(provider="acme-ai", change_fingerprint="a" * 64)
    b = migration_branch_name(provider="acme-ai", change_fingerprint="b" * 64)
    assert a != b


def test_branch_name_sanitizes_unsafe_provider_chars() -> None:
    name = migration_branch_name(provider="Acme AI/Beta!", change_fingerprint="c" * 64)
    assert name.startswith("patchfrog/migrate/acme-ai-beta")
    assert "/" not in name.split("patchfrog/migrate/", 1)[1].split("/", 1)[0]


def test_branch_name_falls_back_when_no_provider() -> None:
    name = migration_branch_name(provider=None, change_fingerprint="d" * 64)
    assert name == "patchfrog/migrate/dependency/" + "d" * 12


def test_verified_is_auto_open() -> None:
    eligibility, _ = determine_eligibility(VerificationOutcome.VERIFIED, policy=MigrationPRPolicy())
    assert eligibility is MigrationPREligibility.AUTO_OPEN


def test_partially_verified_is_not_eligible_by_default() -> None:
    eligibility, reason = determine_eligibility(VerificationOutcome.PARTIALLY_VERIFIED, policy=MigrationPRPolicy())
    assert eligibility is MigrationPREligibility.NOT_ELIGIBLE
    assert "allow_partially_verified" in reason


def test_partially_verified_is_open_with_approval_when_policy_allows() -> None:
    eligibility, reason = determine_eligibility(
        VerificationOutcome.PARTIALLY_VERIFIED, policy=MigrationPRPolicy(allow_partially_verified=True),
    )
    assert eligibility is MigrationPREligibility.OPEN_WITH_OPERATOR_APPROVAL
    assert "visibly marked" in reason


def test_human_required_is_plan_only() -> None:
    eligibility, _ = determine_eligibility(VerificationOutcome.HUMAN_REQUIRED, policy=MigrationPRPolicy())
    assert eligibility is MigrationPREligibility.PLAN_ONLY


def test_unverified_failed_and_regression_are_never_eligible() -> None:
    for outcome in (VerificationOutcome.UNVERIFIED, VerificationOutcome.FAILED, VerificationOutcome.REGRESSION_DETECTED):
        eligibility, _ = determine_eligibility(outcome, policy=MigrationPRPolicy())
        assert eligibility is MigrationPREligibility.NOT_ELIGIBLE


def test_policy_never_relaxes_regression_or_failed() -> None:
    policy = MigrationPRPolicy(allow_partially_verified=True)
    for outcome in (VerificationOutcome.FAILED, VerificationOutcome.REGRESSION_DETECTED, VerificationOutcome.UNVERIFIED):
        eligibility, _ = determine_eligibility(outcome, policy=policy)
        assert eligibility is MigrationPREligibility.NOT_ELIGIBLE
