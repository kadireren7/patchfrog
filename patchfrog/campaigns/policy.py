"""Workspace compatibility policy -- the pure gate (M11.7).

The *semantics* of what a workspace allows live here, in the public engine,
so they are inspectable and testable. Storing a workspace's chosen values
and enforcing them at job time is a hosted-service concern (Cloud).

There are no silent unsafe defaults: an unconfigured workspace detects and
reports, requires fully ``VERIFIED`` evidence, and never publishes without a
human approving it.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from patchfrog.migration_pr.domain import MigrationPREligibility, MigrationPRPolicy


class WatchMode(StrEnum):
    OFF = "off"
    DETECT_ONLY = "detect_only"
    MIGRATE = "migrate"
    MIGRATE_AND_OPEN_PR = "migrate_and_open_pr"


class VerificationRequirement(StrEnum):
    REQUIRE_VERIFIED = "require_verified"
    #: Explicit opt-in only. A PR opened on this basis is always a draft.
    ALLOW_PARTIALLY_VERIFIED = "allow_partially_verified"


class PublicationMode(StrEnum):
    AUTOMATIC = "automatic"
    DRAFT_ONLY = "draft_only"
    MANUAL_APPROVAL = "manual_approval"


class PublicationAction(StrEnum):
    NONE = "none"
    PUBLISH = "publish"
    PUBLISH_DRAFT = "publish_draft"
    AWAIT_APPROVAL = "await_approval"


@dataclass(frozen=True, slots=True)
class WorkspacePolicy:
    watch_mode: WatchMode = WatchMode.DETECT_ONLY
    verification: VerificationRequirement = VerificationRequirement.REQUIRE_VERIFIED
    publication: PublicationMode = PublicationMode.MANUAL_APPROVAL

    @property
    def analyzes(self) -> bool:
        return self.watch_mode is not WatchMode.OFF

    @property
    def migrates(self) -> bool:
        return self.watch_mode in (WatchMode.MIGRATE, WatchMode.MIGRATE_AND_OPEN_PR)

    @property
    def may_publish(self) -> bool:
        return self.watch_mode is WatchMode.MIGRATE_AND_OPEN_PR

    def pr_policy(self) -> MigrationPRPolicy:
        return MigrationPRPolicy(
            allow_partially_verified=self.verification is VerificationRequirement.ALLOW_PARTIALLY_VERIFIED
        )


@dataclass(frozen=True, slots=True)
class PublicationDecision:
    action: PublicationAction
    reason: str

    @property
    def writes_to_github(self) -> bool:
        return self.action in (PublicationAction.PUBLISH, PublicationAction.PUBLISH_DRAFT)

    @property
    def draft(self) -> bool:
        return self.action is PublicationAction.PUBLISH_DRAFT


def decide_publication(policy: WorkspacePolicy, eligibility: MigrationPREligibility) -> PublicationDecision:
    """``eligibility`` must have been computed with :meth:`WorkspacePolicy.pr_policy`;
    this function only layers the workspace's *publication* choices on top."""

    if not policy.may_publish:
        return PublicationDecision(PublicationAction.NONE, f"watch mode is {policy.watch_mode.value}: no PR is opened")
    if eligibility is MigrationPREligibility.PLAN_ONLY:
        return PublicationDecision(PublicationAction.NONE, "human-required migration: report only, never an automatic PR")
    if eligibility is MigrationPREligibility.NOT_ELIGIBLE:
        return PublicationDecision(PublicationAction.NONE, "verification did not meet the workspace requirement")
    partial = eligibility is MigrationPREligibility.OPEN_WITH_OPERATOR_APPROVAL
    if policy.publication is PublicationMode.MANUAL_APPROVAL:
        return PublicationDecision(PublicationAction.AWAIT_APPROVAL, "workspace requires manual approval before a PR is opened")
    if policy.publication is PublicationMode.DRAFT_ONLY:
        return PublicationDecision(PublicationAction.PUBLISH_DRAFT, "workspace publishes draft PRs only")
    if partial:
        return PublicationDecision(
            PublicationAction.PUBLISH_DRAFT, "partially verified: opened as a draft even under automatic publication"
        )
    return PublicationDecision(PublicationAction.PUBLISH, "verified migration; workspace allows automatic publication")


__all__ = [
    "PublicationAction",
    "PublicationDecision",
    "PublicationMode",
    "VerificationRequirement",
    "WatchMode",
    "WorkspacePolicy",
    "decide_publication",
]
