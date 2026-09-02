"""Pure-logic tests for :mod:`patchfrog.telemetry.domain` -- no database,
no LLM. See ``tests/integration/test_telemetry_collector.py`` for the
end-to-end collection tests over real persisted state."""

from __future__ import annotations

from patchfrog.review.domain import CriticDecision, ProposalStatus
from patchfrog.telemetry.domain import (
    TELEMETRY_SCHEMA_VERSION,
    FindingLifecycleOutcome,
    classify_lifecycle_outcome,
)


def test_schema_version_is_a_positive_int() -> None:
    """Milestone-specific bumps are pinned relatively (``> pre-milestone
    value``, with a reason) in each milestone's own versioning test --
    e.g. ``tests/unit/test_contract_intelligence_versioning.py`` -- so
    this test only guards the type/sign here and never needs updating on
    every future additive-field bump. See :mod:`patchfrog.telemetry.domain`'s
    ``TELEMETRY_SCHEMA_VERSION`` docstring for the full history."""

    assert isinstance(TELEMETRY_SCHEMA_VERSION, int)
    assert TELEMETRY_SCHEMA_VERSION > 0


def test_validation_rejected_classifies_correctly() -> None:
    outcome = classify_lifecycle_outcome(status=ProposalStatus.REJECTED_VALIDATION, critic_decision=None)
    assert outcome is FindingLifecycleOutcome.VALIDATION_REJECTED


def test_critic_rejected_classifies_correctly() -> None:
    outcome = classify_lifecycle_outcome(status=ProposalStatus.REJECTED_CRITIC, critic_decision=CriticDecision.REJECT)
    assert outcome is FindingLifecycleOutcome.CRITIC_REJECTED


def test_below_confidence_threshold_classifies_correctly() -> None:
    outcome = classify_lifecycle_outcome(status=ProposalStatus.REJECTED_LOW_CONFIDENCE, critic_decision=None)
    assert outcome is FindingLifecycleOutcome.BELOW_CONFIDENCE_THRESHOLD


def test_suppressed_duplicate_classifies_correctly() -> None:
    outcome = classify_lifecycle_outcome(status=ProposalStatus.SUPPRESSED_DUPLICATE, critic_decision=None)
    assert outcome is FindingLifecycleOutcome.SUPPRESSED_DUPLICATE


def test_suppressed_contradiction_classifies_correctly() -> None:
    outcome = classify_lifecycle_outcome(status=ProposalStatus.SUPPRESSED_CONTRADICTION, critic_decision=None)
    assert outcome is FindingLifecycleOutcome.SUPPRESSED_CONTRADICTION


def test_suppressed_budget_classifies_correctly() -> None:
    outcome = classify_lifecycle_outcome(status=ProposalStatus.SUPPRESSED_BUDGET, critic_decision=None)
    assert outcome is FindingLifecycleOutcome.SUPPRESSED_BUDGET


def test_accepted_with_no_verdict_is_accepted_final() -> None:
    outcome = classify_lifecycle_outcome(status=ProposalStatus.ACCEPTED, critic_decision=None)
    assert outcome is FindingLifecycleOutcome.ACCEPTED_FINAL


def test_accepted_with_accept_verdict_is_accepted_final() -> None:
    outcome = classify_lifecycle_outcome(status=ProposalStatus.ACCEPTED, critic_decision=CriticDecision.ACCEPT)
    assert outcome is FindingLifecycleOutcome.ACCEPTED_FINAL


def test_accepted_with_downgrade_verdict_is_critic_downgraded() -> None:
    """The one non-trivial mapping: a DOWNGRADE verdict still results in
    an ACCEPTED proposal status (the finding survives, just with a
    lowered severity/confidence) -- telemetry must distinguish this from
    a plain accept, per spec section 4."""

    outcome = classify_lifecycle_outcome(status=ProposalStatus.ACCEPTED, critic_decision=CriticDecision.DOWNGRADE)
    assert outcome is FindingLifecycleOutcome.CRITIC_DOWNGRADED


def test_every_proposal_status_has_a_reachable_terminal_outcome() -> None:
    """Never falls back to PROPOSED for any real, persisted status."""

    for status in ProposalStatus:
        outcome = classify_lifecycle_outcome(status=status, critic_decision=None)
        assert outcome is not FindingLifecycleOutcome.PROPOSED, status
