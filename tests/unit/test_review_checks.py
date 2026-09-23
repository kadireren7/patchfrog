from __future__ import annotations

from dataclasses import dataclass, field

from patchfrog.domain.github_check import GitHubCheckRun, GitHubCheckRunInput
from patchfrog.domain.pull_request import PullRequestRef
from patchfrog.merge_readiness.domain import MergeReadinessDecision
from patchfrog.publishing.checks import (
    ReviewCheckPublisher,
    ReviewCheckState,
    ReviewCheckUpdate,
    build_check_input,
)


@dataclass
class _FakeCheckClient:
    checks: list[GitHubCheckRun] = field(default_factory=list)
    creates: list[GitHubCheckRunInput] = field(default_factory=list)
    updates: list[tuple[int, GitHubCheckRunInput]] = field(default_factory=list)

    async def list_check_runs(
        self, *, installation_id: int, ref: PullRequestRef, head_sha: str
    ) -> list[GitHubCheckRun]:
        return list(self.checks)

    async def create_check_run(
        self, *, installation_id: int, ref: PullRequestRef, check: GitHubCheckRunInput
    ) -> GitHubCheckRun:
        self.creates.append(check)
        created = GitHubCheckRun(
            id=41,
            name=check.name,
            head_sha=check.head_sha,
            external_id=check.external_id,
            status=check.status,
            conclusion=check.conclusion,
        )
        self.checks.append(created)
        return created

    async def update_check_run(
        self,
        *,
        installation_id: int,
        ref: PullRequestRef,
        check_run_id: int,
        check: GitHubCheckRunInput,
    ) -> GitHubCheckRun:
        self.updates.append((check_run_id, check))
        return GitHubCheckRun(
            id=check_run_id,
            name=check.name,
            head_sha=check.head_sha,
            external_id=check.external_id,
            status=check.status,
            conclusion=check.conclusion,
        )


_REF = PullRequestRef(owner="octo", repository="repo", number=7)
_SHA = "a" * 40


def test_lifecycle_has_distinct_user_visible_states() -> None:
    expected = {
        ReviewCheckState.QUEUED: ("queued", None),
        ReviewCheckState.RUNNING: ("in_progress", None),
        ReviewCheckState.COMPLETED_WITH_FINDINGS: ("completed", "action_required"),
        ReviewCheckState.COMPLETED_CLEAN: ("completed", "success"),
        ReviewCheckState.PARTIAL: ("completed", "neutral"),
        ReviewCheckState.FAILED: ("completed", "failure"),
        ReviewCheckState.SKIPPED: ("completed", "skipped"),
    }
    for state, (status, conclusion) in expected.items():
        readiness = (
            MergeReadinessDecision.BLOCKED
            if state is ReviewCheckState.COMPLETED_WITH_FINDINGS
            else MergeReadinessDecision.READY
        )
        check = build_check_input(
            ref=_REF,
            head_sha=_SHA,
            update=ReviewCheckUpdate(state=state, accepted_findings=1, merge_readiness=readiness),
        )
        assert check.status.value == status
        assert (check.conclusion.value if check.conclusion else None) == conclusion


def test_clean_result_is_not_indistinguishable_from_failure() -> None:
    clean = build_check_input(
        ref=_REF,
        head_sha=_SHA,
        update=ReviewCheckUpdate(state=ReviewCheckState.COMPLETED_CLEAN),
    )
    failed = build_check_input(
        ref=_REF,
        head_sha=_SHA,
        update=ReviewCheckUpdate(state=ReviewCheckState.FAILED, detail="provider unavailable"),
    )
    assert "No actionable findings" in clean.output.summary
    assert clean.conclusion != failed.conclusion
    assert "provider unavailable" in failed.output.summary


async def test_same_head_reconciles_existing_check_instead_of_spamming() -> None:
    client = _FakeCheckClient()
    publisher = ReviewCheckPublisher(client=client, installation_id=99)
    await publisher.reconcile(
        ref=_REF,
        head_sha=_SHA,
        update=ReviewCheckUpdate(state=ReviewCheckState.QUEUED),
    )
    await publisher.reconcile(
        ref=_REF,
        head_sha=_SHA,
        update=ReviewCheckUpdate(state=ReviewCheckState.COMPLETED_CLEAN),
    )
    assert len(client.creates) == 1
    assert len(client.updates) == 1
    assert client.updates[0][0] == 41


async def test_new_head_creates_distinct_check_identity() -> None:
    client = _FakeCheckClient()
    publisher = ReviewCheckPublisher(client=client, installation_id=99)
    await publisher.reconcile(
        ref=_REF,
        head_sha=_SHA,
        update=ReviewCheckUpdate(state=ReviewCheckState.SKIPPED, detail="superseded head"),
    )
    await publisher.reconcile(
        ref=_REF,
        head_sha="b" * 40,
        update=ReviewCheckUpdate(state=ReviewCheckState.QUEUED),
    )
    assert len(client.creates) == 2
    assert client.creates[0].external_id != client.creates[1].external_id
