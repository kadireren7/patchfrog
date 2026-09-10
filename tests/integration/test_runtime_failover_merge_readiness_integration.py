"""End-to-end proof that runtime provider failover integrates correctly
with Merge Readiness (Milestone U pre-merge correction, Blocker 1's own
explicit requirement): a real review run through
:class:`~patchfrog.review.service.PullRequestReviewService` whose every
candidate's specialist call fails (with no runtime fallback configured)
must never let :class:`~patchfrog.merge_readiness.service.MergeReadinessService`
see it as a clean pass. Provider failure that prevents required review
completion must still result in ``HUMAN_REVIEW_REQUIRED``, never
``READY`` -- "no findings because it didn't run" must never look like
"no findings because it ran and found none."."""

from __future__ import annotations

import uuid
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from patchfrog.diff.models import DiffFile, DiffHunk, DiffLine, DiffLineType
from patchfrog.indexing.service import RepositoryIndexingService
from patchfrog.merge_readiness.domain import MergeReadinessDecision, MergeReadinessReasonCode
from patchfrog.merge_readiness.service import MergeReadinessService
from patchfrog.persistence.models.review import ReviewRunModel
from patchfrog.persistence.repositories import RepositoryRepository
from patchfrog.persistence.repositories.pull_request import PullRequestRepository
from patchfrog.review.agents.roles import AgentRole
from patchfrog.review.domain import ReviewRunStatus
from patchfrog.review.provider import ProviderTransientError
from patchfrog.review.providers.fake import FakeLLMProvider, ScriptedResponse
from patchfrog.review.service import PullRequestReviewService
from patchfrog.routing.domain import ReviewRoutePlan, RouteReason
from tests.support.git_repo import materialize_fixture_repo


def _diff_marking_lines(file_path: str, lines: list[int]) -> DiffFile:
    diff_lines = tuple(
        DiffLine(line_type=DiffLineType.ADDITION, old_line_number=None, new_line_number=n, content="x")
        for n in lines
    )
    hunk = DiffHunk(
        old_start=1, old_lines=0, new_start=min(lines), new_lines=len(lines),
        section_heading=None, lines=diff_lines,
    )
    return DiffFile(path=file_path, hunks=(hunk,))


async def _setup(
    session_factory: async_sessionmaker[AsyncSession], *, full_name: str
) -> tuple[uuid.UUID, str, Path]:
    async with session_factory() as session:
        row = await RepositoryRepository().upsert(
            session, github_repository_id=abs(hash(full_name)) % (2**62),
            owner="test", name=full_name.split("/")[-1], full_name=full_name, installation_id=0,
        )
        await session.commit()
        repository_id = row.id

    root = Path("/tmp") / f"pf-runtime-failover-{uuid.uuid4().hex[:8]}"
    snapshot = materialize_fixture_repo(root, "ai_review_python", full_name=full_name)
    await RepositoryIndexingService(session_factory=session_factory).index_local_repository(
        repository_id=repository_id, root_path=snapshot.root_path, repository_full_name=full_name
    )
    return repository_id, snapshot.commit_sha, snapshot.root_path


async def test_all_specialist_calls_failing_with_no_fallback_never_produces_a_false_ready(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    full_name = "test/runtime-failover-no-fallback"
    repository_id, commit_sha, root_path = await _setup(session_factory, full_name=full_name)
    diff_files = [_diff_marking_lines("src/billing.py", [14])]

    async with session_factory() as session:
        pull_request = await PullRequestRepository().upsert(
            session, repository_id=repository_id, github_pr_number=1, title="t", author="a",
            base_sha=commit_sha, head_sha=commit_sha, state="open",
        )
        await session.commit()
        pull_request_id = pull_request.id

    # Every specialist call fails transiently, and no runtime fallback
    # provider is configured (reviewer_provider= alone, no route_plan) --
    # every candidate's every role must fail honestly.
    provider = FakeLLMProvider([ProviderTransientError("down")] * 20)
    service = PullRequestReviewService(session_factory=session_factory, reviewer_provider=provider)

    summary = await service.review_local(
        repository_id=repository_id, root_path=root_path, repository_full_name=full_name,
        commit_sha=commit_sha, diff_files=diff_files, pull_request_id=pull_request_id,
    )

    async with session_factory() as session:
        run = await session.get(ReviewRunModel, summary.run_id)
        assert run is not None
        # The run must never be reported SUCCEEDED when every candidate's
        # review work failed -- "zero findings" here is not a clean pass.
        assert run.status is not ReviewRunStatus.SUCCEEDED

    async with session_factory() as session:
        result = await MergeReadinessService().evaluate(
            session, repository_id=repository_id, pull_request_number=1
        )
    assert result is not None
    assert result.decision is MergeReadinessDecision.HUMAN_REVIEW_REQUIRED
    assert MergeReadinessReasonCode.REVIEW_INCOMPLETE in result.reason_codes


async def test_primary_failure_with_successful_fallback_completes_and_readiness_evaluates_normally(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    full_name = "test/runtime-failover-with-fallback"
    repository_id, commit_sha, root_path = await _setup(session_factory, full_name=full_name)
    diff_files = [_diff_marking_lines("src/billing.py", [14])]

    async with session_factory() as session:
        pull_request = await PullRequestRepository().upsert(
            session, repository_id=repository_id, github_pr_number=1, title="t", author="a",
            base_sha=commit_sha, head_sha=commit_sha, state="open",
        )
        await session.commit()
        pull_request_id = pull_request.id

    primary = FakeLLMProvider([ProviderTransientError("down")] * 20, provider_name="primary")
    fallback = FakeLLMProvider(
        response_factory=lambda _req: ScriptedResponse(raw_json='{"findings": []}'), provider_name="fallback",
    )
    route_plan = ReviewRoutePlan(
        reviewer_providers={AgentRole.CORRECTNESS: primary, AgentRole.SECURITY: primary},
        critic_provider=None,
        reviewer_provider_family="primary",
        critic_provider_family=None,
        reasons=(RouteReason.SINGLE_PROVIDER_CONFIGURED,),
        diversity_available=False,
        diversity_used=False,
        config_fallback_used=False,
        reviewer_fallback_providers={AgentRole.CORRECTNESS: fallback, AgentRole.SECURITY: fallback},
        reviewer_runtime_fallback_family="fallback",
        runtime_fallback_permitted=True,
    )
    service = PullRequestReviewService(session_factory=session_factory, route_plan=route_plan)

    summary = await service.review_local(
        repository_id=repository_id, root_path=root_path, repository_full_name=full_name,
        commit_sha=commit_sha, diff_files=diff_files, pull_request_id=pull_request_id,
    )

    async with session_factory() as session:
        run = await session.get(ReviewRunModel, summary.run_id)
        assert run is not None
        assert run.status is ReviewRunStatus.SUCCEEDED

    async with session_factory() as session:
        result = await MergeReadinessService().evaluate(
            session, repository_id=repository_id, pull_request_number=1
        )
    assert result is not None
    # A completed review with no unresolved evidence evaluates exactly
    # as it would have if the primary had never failed at all.
    assert result.decision is MergeReadinessDecision.READY
