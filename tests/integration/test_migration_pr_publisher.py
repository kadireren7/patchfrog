"""M9.6 / M9.7 / M9.8 / M9.9: migration PR publication -- idempotency,
stale-base protection, eligibility gating, and check-run reconciliation,
all against :class:`FakeMigrationGitHubPublisher` (M9.9's own
requirement: no real GitHub PR is ever opened by an automated test).
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from patchfrog.migration.domain import (
    GeneratedPatch,
    PatchOrigin,
    SafetyGateResult,
    StepOutcome,
    StepResult,
)
from patchfrog.migration_pr.domain import (
    MigrationPREligibility,
    MigrationPRLinkage,
    MigrationPRPlan,
    MigrationPRPolicy,
    MigrationPRStatus,
)
from patchfrog.migration_pr.eligibility import determine_eligibility
from patchfrog.migration_pr.fake_github import FakeMigrationGitHubPublisher
from patchfrog.migration_pr.publisher import MigrationPRPublicationMode, MigrationPRPublisher
from patchfrog.migration_verification.domain import VerificationOutcome

BASE_SHA = "a" * 40
OWNER, REPO = "octo", "widgets"
FULL_NAME = f"{OWNER}/{REPO}"


def _linkage(*, change_fingerprint: str = "c" * 64, patch_fingerprint: str = "p" * 64, base_sha: str = BASE_SHA) -> MigrationPRLinkage:
    return MigrationPRLinkage(
        change_fingerprint=change_fingerprint, dependency_key="acme-ai:pypi", repository=FULL_NAME,
        base_commit_sha=base_sha, plan_fingerprint="pl" * 32, patch_fingerprint=patch_fingerprint,
        verification_plan_fingerprint="v" * 64, bundle_fingerprint="e" * 64,
    )


def _plan(*, outcome: VerificationOutcome, policy: MigrationPRPolicy | None = None, linkage: MigrationPRLinkage | None = None) -> MigrationPRPlan:
    linkage = linkage or _linkage()
    eligibility, reason = determine_eligibility(outcome, policy=policy or MigrationPRPolicy())
    return MigrationPRPlan(
        linkage=linkage, eligibility=eligibility, eligibility_reason=reason,
        branch_name="patchfrog/migrate/acme-ai/" + linkage.change_fingerprint[:12], commit_message="migrate acme-ai",
        pr_title="PatchFrog: migrate acme-ai", pr_body="dossier body", check_title="check title",
        check_summary="check summary",
    )


def _patch(*, content: str = "new content") -> GeneratedPatch:
    return GeneratedPatch(
        origin=PatchOrigin.DETERMINISTIC, unified_diff="diff", modified_files=("app/chat.py",),
        new_contents={"app/chat.py": content}, step_results=(StepResult("s1", StepOutcome.APPLIED, "d"),),
        safety=(SafetyGateResult("g", True, "ok"),),
    )


def _publisher(session_factory: async_sessionmaker[AsyncSession], fake: FakeMigrationGitHubPublisher) -> MigrationPRPublisher:
    return MigrationPRPublisher(session_factory=session_factory, publisher=fake, installation_id=1)


async def test_dry_run_makes_no_github_call(session_factory: async_sessionmaker[AsyncSession]) -> None:
    fake = FakeMigrationGitHubPublisher()
    fake.set_ref(owner=OWNER, repository=REPO, ref="heads/main", sha=BASE_SHA)
    plan = _plan(outcome=VerificationOutcome.VERIFIED)
    result = await _publisher(session_factory, fake).publish(plan=plan, patch=_patch(), mode=MigrationPRPublicationMode.DRY_RUN)
    assert result.status is MigrationPRStatus.DRY_RUN
    assert fake.create_ref_calls == [] and fake.create_pull_request_calls == []


async def test_verified_migration_opens_a_pr(session_factory: async_sessionmaker[AsyncSession]) -> None:
    fake = FakeMigrationGitHubPublisher()
    fake.set_ref(owner=OWNER, repository=REPO, ref="heads/main", sha=BASE_SHA)
    plan = _plan(outcome=VerificationOutcome.VERIFIED)
    result = await _publisher(session_factory, fake).publish(plan=plan, patch=_patch(), mode=MigrationPRPublicationMode.PUBLISH)
    assert result.status is MigrationPRStatus.OPENED
    assert result.number is not None
    assert fake.create_ref_calls == [f"refs/heads/{plan.branch_name}"]
    assert len(fake.checks) == 1 and fake.checks[0].conclusion is not None


async def test_republishing_identical_patch_is_idempotent_updates_same_pr(session_factory: async_sessionmaker[AsyncSession]) -> None:
    fake = FakeMigrationGitHubPublisher()
    fake.set_ref(owner=OWNER, repository=REPO, ref="heads/main", sha=BASE_SHA)
    plan = _plan(outcome=VerificationOutcome.VERIFIED)
    publisher = _publisher(session_factory, fake)
    first = await publisher.publish(plan=plan, patch=_patch(), mode=MigrationPRPublicationMode.PUBLISH)
    second = await publisher.publish(plan=plan, patch=_patch(), mode=MigrationPRPublicationMode.PUBLISH)
    assert first.number == second.number
    assert second.status is MigrationPRStatus.UPDATED
    assert len(fake.create_pull_request_calls) == 1  # never a duplicate PR


async def test_regenerated_patch_updates_the_same_branch_and_pr(session_factory: async_sessionmaker[AsyncSession]) -> None:
    fake = FakeMigrationGitHubPublisher()
    fake.set_ref(owner=OWNER, repository=REPO, ref="heads/main", sha=BASE_SHA)
    plan = _plan(outcome=VerificationOutcome.VERIFIED)
    publisher = _publisher(session_factory, fake)
    first = await publisher.publish(plan=plan, patch=_patch(content="v1"), mode=MigrationPRPublicationMode.PUBLISH)
    second = await publisher.publish(plan=plan, patch=_patch(content="v2 -- different"), mode=MigrationPRPublicationMode.PUBLISH)
    assert first.number == second.number
    assert fake.update_ref_calls == [f"heads/{plan.branch_name}"]  # force-moved, never a second create_ref


async def test_stale_base_never_publishes(session_factory: async_sessionmaker[AsyncSession]) -> None:
    fake = FakeMigrationGitHubPublisher()
    fake.set_ref(owner=OWNER, repository=REPO, ref="heads/main", sha="moved" + "0" * 35)
    plan = _plan(outcome=VerificationOutcome.VERIFIED)  # linkage.base_commit_sha is BASE_SHA, not the moved one
    result = await _publisher(session_factory, fake).publish(plan=plan, patch=_patch(), mode=MigrationPRPublicationMode.PUBLISH)
    assert result.status is MigrationPRStatus.STALE_REQUIRES_REGENERATION
    assert fake.create_ref_calls == [] and fake.create_pull_request_calls == []


async def test_failed_outcome_never_opens_a_pr(session_factory: async_sessionmaker[AsyncSession]) -> None:
    fake = FakeMigrationGitHubPublisher()
    fake.set_ref(owner=OWNER, repository=REPO, ref="heads/main", sha=BASE_SHA)
    plan = _plan(outcome=VerificationOutcome.FAILED)
    result = await _publisher(session_factory, fake).publish(plan=plan, patch=_patch(), mode=MigrationPRPublicationMode.PUBLISH)
    assert result.status is MigrationPRStatus.SKIPPED_NOT_ELIGIBLE
    assert fake.create_ref_calls == []


async def test_regression_detected_never_opens_a_pr(session_factory: async_sessionmaker[AsyncSession]) -> None:
    fake = FakeMigrationGitHubPublisher()
    fake.set_ref(owner=OWNER, repository=REPO, ref="heads/main", sha=BASE_SHA)
    plan = _plan(outcome=VerificationOutcome.REGRESSION_DETECTED)
    result = await _publisher(session_factory, fake).publish(plan=plan, patch=_patch(), mode=MigrationPRPublicationMode.PUBLISH)
    assert result.status is MigrationPRStatus.SKIPPED_NOT_ELIGIBLE


async def test_partially_verified_without_policy_never_opens_a_pr(session_factory: async_sessionmaker[AsyncSession]) -> None:
    fake = FakeMigrationGitHubPublisher()
    fake.set_ref(owner=OWNER, repository=REPO, ref="heads/main", sha=BASE_SHA)
    plan = _plan(outcome=VerificationOutcome.PARTIALLY_VERIFIED)
    result = await _publisher(session_factory, fake).publish(plan=plan, patch=_patch(), mode=MigrationPRPublicationMode.PUBLISH)
    assert result.status is MigrationPRStatus.SKIPPED_NOT_ELIGIBLE


async def test_partially_verified_with_operator_policy_opens_a_marked_pr(session_factory: async_sessionmaker[AsyncSession]) -> None:
    fake = FakeMigrationGitHubPublisher()
    fake.set_ref(owner=OWNER, repository=REPO, ref="heads/main", sha=BASE_SHA)
    plan = _plan(outcome=VerificationOutcome.PARTIALLY_VERIFIED, policy=MigrationPRPolicy(allow_partially_verified=True))
    assert plan.eligibility is MigrationPREligibility.OPEN_WITH_OPERATOR_APPROVAL
    result = await _publisher(session_factory, fake).publish(plan=plan, patch=_patch(), mode=MigrationPRPublicationMode.PUBLISH)
    assert result.status is MigrationPRStatus.OPENED


async def test_human_required_never_touches_github(session_factory: async_sessionmaker[AsyncSession]) -> None:
    fake = FakeMigrationGitHubPublisher()
    fake.set_ref(owner=OWNER, repository=REPO, ref="heads/main", sha=BASE_SHA)
    plan = _plan(outcome=VerificationOutcome.HUMAN_REQUIRED)
    assert plan.eligibility is MigrationPREligibility.PLAN_ONLY
    result = await _publisher(session_factory, fake).publish(plan=plan, patch=_patch(), mode=MigrationPRPublicationMode.PUBLISH)
    assert result.status is MigrationPRStatus.PLANNED
    assert fake.create_ref_calls == [] and fake.create_pull_request_calls == []


async def test_closed_pr_is_never_reopened_automatically(session_factory: async_sessionmaker[AsyncSession]) -> None:
    fake = FakeMigrationGitHubPublisher()
    fake.set_ref(owner=OWNER, repository=REPO, ref="heads/main", sha=BASE_SHA)
    plan = _plan(outcome=VerificationOutcome.VERIFIED)
    publisher = _publisher(session_factory, fake)
    first = await publisher.publish(plan=plan, patch=_patch(), mode=MigrationPRPublicationMode.PUBLISH)
    assert first.number is not None
    fake.close_pull_request(first.number)

    second = await publisher.publish(plan=plan, patch=_patch(content="v2"), mode=MigrationPRPublicationMode.PUBLISH)
    assert second.status is MigrationPRStatus.NO_OP_UNCHANGED
    assert second.number == first.number
    assert len(fake.create_pull_request_calls) == 1  # never reopened, never duplicated


async def test_check_run_is_reconciled_not_duplicated(session_factory: async_sessionmaker[AsyncSession]) -> None:
    fake = FakeMigrationGitHubPublisher()
    fake.set_ref(owner=OWNER, repository=REPO, ref="heads/main", sha=BASE_SHA)
    plan = _plan(outcome=VerificationOutcome.VERIFIED)
    publisher = _publisher(session_factory, fake)
    # A GitHub check run lives on one exact commit SHA -- republishing
    # identical patch content produces the identical tree/commit (never a
    # new SHA), so this is the one case reconciliation actually applies:
    # same external_id *and* same head_sha both times.
    await publisher.publish(plan=plan, patch=_patch(content="same"), mode=MigrationPRPublicationMode.PUBLISH)
    await publisher.publish(plan=plan, patch=_patch(content="same"), mode=MigrationPRPublicationMode.PUBLISH)
    assert len(fake.checks) == 1


async def test_regenerated_patch_gets_its_own_check_run_on_its_own_commit(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A force-moved branch's new commit is a different SHA -- GitHub
    check runs are inherently per-commit, so a *changed* patch legitimately
    produces a second check run (on the new tip), never an update to a
    check attached to a commit that is no longer the branch head."""

    fake = FakeMigrationGitHubPublisher()
    fake.set_ref(owner=OWNER, repository=REPO, ref="heads/main", sha=BASE_SHA)
    plan = _plan(outcome=VerificationOutcome.VERIFIED)
    publisher = _publisher(session_factory, fake)
    await publisher.publish(plan=plan, patch=_patch(content="v1"), mode=MigrationPRPublicationMode.PUBLISH)
    await publisher.publish(plan=plan, patch=_patch(content="v2"), mode=MigrationPRPublicationMode.PUBLISH)
    assert len(fake.checks) == 2
    assert len({c.head_sha for c in fake.checks}) == 2
