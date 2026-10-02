"""Idempotent GitHub Check Run lifecycle for one migration branch head
(M9.5) -- reuses the existing :class:`patchfrog.publishing.checks.CheckRunClient`
Protocol and :class:`GitHubClient` verbatim; this is a second, parallel
check *name*/external-id scheme, never a second check-run publishing
mechanism.
"""

from __future__ import annotations

from patchfrog.domain.github_check import (
    GitHubCheckConclusion,
    GitHubCheckOutput,
    GitHubCheckRun,
    GitHubCheckRunInput,
    GitHubCheckStatus,
)
from patchfrog.domain.pull_request import PullRequestRef
from patchfrog.migration_pr.domain import MigrationPREligibility
from patchfrog.migration_verification.domain import VerificationOutcome
from patchfrog.publishing.checks import CheckRunClient

CHECK_NAME = "PatchFrog Migration Verification"


def build_external_id(*, owner: str, repository: str, change_fingerprint: str, patch_fingerprint: str) -> str:
    return f"patchfrog-migration:{owner}/{repository}:{change_fingerprint}:{patch_fingerprint}"


def build_check_input(
    *,
    head_sha: str,
    external_id: str,
    outcome: VerificationOutcome | None,
    eligibility: MigrationPREligibility | None,
    title: str,
    summary: str,
    details_url: str | None = None,
) -> GitHubCheckRunInput:
    status, conclusion = _presentation(outcome, eligibility)
    return GitHubCheckRunInput(
        name=CHECK_NAME, head_sha=head_sha, external_id=external_id, status=status, conclusion=conclusion,
        output=GitHubCheckOutput(title=title, summary=summary), details_url=details_url,
    )


def _presentation(
    outcome: VerificationOutcome | None, eligibility: MigrationPREligibility | None
) -> tuple[GitHubCheckStatus, GitHubCheckConclusion | None]:
    if outcome is None:
        return GitHubCheckStatus.IN_PROGRESS, None
    if outcome is VerificationOutcome.VERIFIED:
        return GitHubCheckStatus.COMPLETED, GitHubCheckConclusion.SUCCESS
    if outcome is VerificationOutcome.PARTIALLY_VERIFIED:
        conclusion = (
            GitHubCheckConclusion.NEUTRAL if eligibility is MigrationPREligibility.OPEN_WITH_OPERATOR_APPROVAL
            else GitHubCheckConclusion.ACTION_REQUIRED
        )
        return GitHubCheckStatus.COMPLETED, conclusion
    if outcome is VerificationOutcome.HUMAN_REQUIRED:
        return GitHubCheckStatus.COMPLETED, GitHubCheckConclusion.NEUTRAL
    if outcome is VerificationOutcome.REGRESSION_DETECTED:
        return GitHubCheckStatus.COMPLETED, GitHubCheckConclusion.FAILURE
    if outcome is VerificationOutcome.FAILED:
        return GitHubCheckStatus.COMPLETED, GitHubCheckConclusion.FAILURE
    return GitHubCheckStatus.COMPLETED, GitHubCheckConclusion.NEUTRAL  # UNVERIFIED


class MigrationCheckPublisher:
    def __init__(self, *, client: CheckRunClient, installation_id: int) -> None:
        self._client = client
        self._installation_id = installation_id

    async def reconcile(
        self, *, owner: str, repository: str, head_sha: str, check: GitHubCheckRunInput
    ) -> GitHubCheckRun:
        ref = PullRequestRef(owner=owner, repository=repository, number=0)
        existing = await self._client.list_check_runs(installation_id=self._installation_id, ref=ref, head_sha=head_sha)
        match = next((item for item in existing if item.name == CHECK_NAME and item.external_id == check.external_id), None)
        if match is None:
            return await self._client.create_check_run(installation_id=self._installation_id, ref=ref, check=check)
        return await self._client.update_check_run(
            installation_id=self._installation_id, ref=ref, check_run_id=match.id, check=check,
        )


__all__ = ["CHECK_NAME", "MigrationCheckPublisher", "build_check_input", "build_external_id"]
