"""Idempotent GitHub Check Run lifecycle for one PR head.

This is presentation only: review and merge-readiness decisions continue
to come from the public engine.  The stable external id lets a synchronize
retry update the same check instead of creating duplicate comments/checks.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from patchfrog.domain.github_check import (
    GitHubCheckConclusion,
    GitHubCheckOutput,
    GitHubCheckRun,
    GitHubCheckRunInput,
    GitHubCheckStatus,
)
from patchfrog.domain.pull_request import PullRequestRef
from patchfrog.github.client import GitHubClient
from patchfrog.merge_readiness.domain import MergeReadinessDecision

CHECK_NAME = "PatchFrog review"


class ReviewCheckState(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED_WITH_FINDINGS = "completed_with_findings"
    COMPLETED_CLEAN = "completed_clean"
    PARTIAL = "partial"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass(frozen=True, slots=True)
class ReviewCheckUpdate:
    state: ReviewCheckState
    accepted_findings: int = 0
    detail: str | None = None
    merge_readiness: MergeReadinessDecision | None = None
    details_url: str | None = None


class CheckRunClient(Protocol):
    async def list_check_runs(self, *, installation_id: int, ref: PullRequestRef, head_sha: str) -> list[GitHubCheckRun]: ...

    async def create_check_run(
        self, *, installation_id: int, ref: PullRequestRef, check: GitHubCheckRunInput
    ) -> GitHubCheckRun: ...

    async def update_check_run(
        self, *, installation_id: int, ref: PullRequestRef, check_run_id: int, check: GitHubCheckRunInput
    ) -> GitHubCheckRun: ...


class ReviewCheckPublisher:
    def __init__(self, *, client: CheckRunClient, installation_id: int) -> None:
        self._client = client
        self._installation_id = installation_id

    async def reconcile(
        self,
        *,
        ref: PullRequestRef,
        head_sha: str,
        update: ReviewCheckUpdate,
    ) -> GitHubCheckRun:
        check = build_check_input(ref=ref, head_sha=head_sha, update=update)
        existing = await self._client.list_check_runs(
            installation_id=self._installation_id,
            ref=ref,
            head_sha=head_sha,
        )
        match = next(
            (item for item in existing if item.name == CHECK_NAME and item.external_id == check.external_id),
            None,
        )
        if match is None:
            return await self._client.create_check_run(
                installation_id=self._installation_id,
                ref=ref,
                check=check,
            )
        return await self._client.update_check_run(
            installation_id=self._installation_id,
            ref=ref,
            check_run_id=match.id,
            check=check,
        )


def build_check_input(
    *, ref: PullRequestRef, head_sha: str, update: ReviewCheckUpdate
) -> GitHubCheckRunInput:
    external_id = f"patchfrog-review:{ref.owner}/{ref.repository}:{ref.number}:{head_sha}"
    status, conclusion, title, summary = _presentation(update)
    return GitHubCheckRunInput(
        name=CHECK_NAME,
        head_sha=head_sha,
        external_id=external_id,
        status=status,
        conclusion=conclusion,
        output=GitHubCheckOutput(title=title, summary=summary),
        details_url=update.details_url,
    )


def _presentation(
    update: ReviewCheckUpdate,
) -> tuple[GitHubCheckStatus, GitHubCheckConclusion | None, str, str]:
    detail = update.detail
    if update.state is ReviewCheckState.QUEUED:
        return GitHubCheckStatus.QUEUED, None, "PatchFrog review queued", detail or "Waiting to start."
    if update.state is ReviewCheckState.RUNNING:
        return GitHubCheckStatus.IN_PROGRESS, None, "PatchFrog is reviewing this change", detail or "Review is in progress."
    if update.state is ReviewCheckState.FAILED:
        return GitHubCheckStatus.COMPLETED, GitHubCheckConclusion.FAILURE, "PatchFrog review failed", detail or "The review did not complete."
    if update.state is ReviewCheckState.SKIPPED:
        return GitHubCheckStatus.COMPLETED, GitHubCheckConclusion.SKIPPED, "PatchFrog review skipped", detail or "This head was not reviewed."
    if update.state is ReviewCheckState.PARTIAL:
        return GitHubCheckStatus.COMPLETED, GitHubCheckConclusion.NEUTRAL, "PatchFrog review is partial", detail or "Some review work could not complete."

    readiness = update.merge_readiness
    if readiness is MergeReadinessDecision.BLOCKED:
        conclusion = GitHubCheckConclusion.ACTION_REQUIRED
    elif readiness is MergeReadinessDecision.HUMAN_REVIEW_REQUIRED:
        conclusion = GitHubCheckConclusion.NEUTRAL
    else:
        conclusion = GitHubCheckConclusion.SUCCESS
    readiness_text = f" Merge readiness: {readiness.value}." if readiness is not None else ""
    if update.state is ReviewCheckState.COMPLETED_CLEAN:
        return (
            GitHubCheckStatus.COMPLETED,
            conclusion,
            "PatchFrog found no actionable findings",
            (detail or "PatchFrog reviewed this change. No actionable findings were accepted.") + readiness_text,
        )
    return (
        GitHubCheckStatus.COMPLETED,
        conclusion,
        f"PatchFrog accepted {update.accepted_findings} finding(s)",
        (detail or "Review completed with actionable findings.") + readiness_text,
    )


def github_check_publisher(*, client: GitHubClient, installation_id: int) -> ReviewCheckPublisher:
    return ReviewCheckPublisher(client=client, installation_id=installation_id)
