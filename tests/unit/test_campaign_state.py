"""M10.3: deterministic repository and campaign state semantics."""

from __future__ import annotations

import pytest

from patchfrog.campaigns.domain import CampaignState, Freshness, OrgClass, RepoState
from patchfrog.campaigns.state import RepoFacts, derive_campaign_state, derive_repo_state

F, A, N, U = Freshness.FRESH, OrgClass.AFFECTED, OrgClass.NOT_AFFECTED, OrgClass.UNKNOWN


@pytest.mark.parametrize(
    ("facts", "expected"),
    [
        (RepoFacts(Freshness.ACCESS_LOST, A), RepoState.ACCESS_LOST),
        (RepoFacts(Freshness.STALE, N), RepoState.STALE),
        (RepoFacts(Freshness.UNKNOWN, N), RepoState.UNKNOWN),
        (RepoFacts(F, N), RepoState.NOT_AFFECTED),
        (RepoFacts(F, N, ever_affected=True), RepoState.RESOLVED),
        (RepoFacts(F, U), RepoState.UNKNOWN),
        (RepoFacts(F, A), RepoState.IMPACTED),
        (RepoFacts(F, A, migration_status="planned", has_automatic_steps=True), RepoState.MIGRATION_PLANNED),
        (RepoFacts(F, A, migration_status="planned", has_automatic_steps=False), RepoState.HUMAN_REQUIRED),
        (RepoFacts(F, A, migration_status="human_required"), RepoState.HUMAN_REQUIRED),
        (RepoFacts(F, A, migration_status="unsupported"), RepoState.HUMAN_REQUIRED),
        (RepoFacts(F, A, migration_status="failed"), RepoState.FAILED),
        (RepoFacts(F, A, migration_status="patch_generated", has_automatic_steps=True, patch_generated=True),
         RepoState.PATCH_GENERATED),
        (RepoFacts(F, A, verification_outcome="verified"), RepoState.VERIFIED),
        (RepoFacts(F, A, verification_outcome="partially_verified"), RepoState.PARTIALLY_VERIFIED),
        (RepoFacts(F, A, verification_outcome="unverified"), RepoState.UNVERIFIED),
        (RepoFacts(F, A, verification_outcome="failed"), RepoState.FAILED),
        (RepoFacts(F, A, verification_outcome="regression_detected"), RepoState.FAILED),
        (RepoFacts(F, A, verification_outcome="human_required"), RepoState.HUMAN_REQUIRED),
        (RepoFacts(F, A, verification_outcome="verified", pr_status="opened", pr_number=7), RepoState.PR_OPENED),
        (RepoFacts(F, A, verification_outcome="verified", pr_status="stale_requires_regeneration"),
         RepoState.VERIFIED),
        (RepoFacts(F, A, failed=True), RepoState.FAILED),
        (RepoFacts(F, A, verification_outcome="verified", pr_status="updated", pr_number=7), RepoState.PR_OPENED),
        # M9 never reopens a PR a human closed -> a human decision, not "in flight"
        (RepoFacts(F, A, verification_outcome="verified", pr_status="no_op_unchanged", pr_number=7),
         RepoState.HUMAN_REQUIRED),
    ],
)
def test_repo_state_matrix(facts: RepoFacts, expected: RepoState) -> None:
    assert derive_repo_state(facts) is expected


def test_stale_evidence_beats_everything_else_but_failure() -> None:
    # Even a repository that would be a verified, PR-opened migration is STALE if its evidence is not fresh.
    facts = RepoFacts(Freshness.STALE, A, verification_outcome="verified", pr_status="opened", pr_number=1)
    assert derive_repo_state(facts) is RepoState.STALE


def test_open_pr_never_hides_a_failed_or_human_outcome() -> None:
    assert derive_repo_state(RepoFacts(F, A, verification_outcome="human_required", pr_status="opened", pr_number=1)) \
        is RepoState.HUMAN_REQUIRED
    assert derive_repo_state(RepoFacts(F, A, verification_outcome="failed", pr_status="opened", pr_number=1)) \
        is RepoState.FAILED


S = RepoState


@pytest.mark.parametrize(
    ("states", "expected"),
    [
        ([], CampaignState.DETECTED),
        ([None, None], CampaignState.DETECTED),
        ([S.NOT_AFFECTED, None], CampaignState.ANALYZING),
        ([S.NOT_AFFECTED, S.NOT_AFFECTED], CampaignState.RESOLVED),
        ([S.RESOLVED, S.NOT_AFFECTED], CampaignState.RESOLVED),
        ([S.VERIFIED, S.NOT_AFFECTED], CampaignState.MIGRATING),
        ([S.PR_OPENED, S.PATCH_GENERATED], CampaignState.MIGRATING),
        ([S.HUMAN_REQUIRED, S.NOT_AFFECTED], CampaignState.ACTION_REQUIRED),
        ([S.VERIFIED, S.HUMAN_REQUIRED, S.NOT_AFFECTED, S.STALE], CampaignState.ACTION_REQUIRED),
        ([S.STALE], CampaignState.ACTION_REQUIRED),
        ([S.UNKNOWN, S.NOT_AFFECTED], CampaignState.ACTION_REQUIRED),
        ([S.PARTIALLY_VERIFIED], CampaignState.ACTION_REQUIRED),
        ([S.UNVERIFIED], CampaignState.ACTION_REQUIRED),
        ([S.RESOLVED, S.HUMAN_REQUIRED], CampaignState.PARTIALLY_RESOLVED),
        ([S.RESOLVED, S.PR_OPENED], CampaignState.PARTIALLY_RESOLVED),
        ([S.RESOLVED, S.STALE, S.NOT_AFFECTED], CampaignState.PARTIALLY_RESOLVED),
        ([S.FAILED, S.NOT_AFFECTED], CampaignState.BLOCKED),
        ([S.FAILED, S.ACCESS_LOST], CampaignState.BLOCKED),
        # failed alongside a human-actionable repo is still actionable, not "blocked"
        ([S.FAILED, S.HUMAN_REQUIRED], CampaignState.ACTION_REQUIRED),
        # failed with other work still in flight is not blocked
        ([S.FAILED, S.VERIFIED], CampaignState.ACTION_REQUIRED),
    ],
)
def test_campaign_state_matrix(states: list[RepoState | None], expected: CampaignState) -> None:
    assert derive_campaign_state(states) is expected


@pytest.mark.parametrize("blocker", [S.STALE, S.UNKNOWN, S.ACCESS_LOST, S.FAILED, S.UNVERIFIED, S.PARTIALLY_VERIFIED,
                                     S.HUMAN_REQUIRED])
def test_a_campaign_is_never_resolved_while_any_repo_is_unresolved(blocker: RepoState) -> None:
    assert derive_campaign_state([S.RESOLVED, S.NOT_AFFECTED, blocker]) is not CampaignState.RESOLVED
    assert derive_campaign_state([S.NOT_AFFECTED, blocker]) is not CampaignState.RESOLVED


def test_impacted_without_a_migration_needs_action_not_progress() -> None:
    assert derive_campaign_state([S.IMPACTED, S.NOT_AFFECTED]) is CampaignState.ACTION_REQUIRED
