"""End-to-end AI Reviewer coverage against the real ``ai_review_python``
fixture, using :class:`FakeLLMProvider` for fully deterministic reviewer
and critic responses. This is the primary regression test proving the
whole pipeline -- candidate generation, context building, prompt
construction, validation, critic review, confidence aggregation, dedup,
and persistence -- produces the right end state."""

from __future__ import annotations

import json
import uuid
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from patchfrog.diff.models import DiffFile, DiffHunk, DiffLine, DiffLineType
from patchfrog.indexing.service import RepositoryIndexingService
from patchfrog.persistence.models.review import ReviewCandidateStatus, ReviewRunModel
from patchfrog.persistence.repositories import (
    AIFindingProposalRepository,
    RepositoryRepository,
    ReviewCandidateRepository,
)
from patchfrog.review.config import ReviewConfig
from patchfrog.review.domain import ProposalStatus, ReviewRunStatus
from patchfrog.review.provider import ProviderRequest
from patchfrog.review.providers.fake import FakeLLMProvider, ScriptedResponse
from patchfrog.review.service import PullRequestReviewService
from tests.support.git_repo import materialize_fixture_repo

_CAN_WITHDRAW_FINDING = {
    "title": "Inverted comparison in can_withdraw",
    "message": "amount >= balance allows a withdrawal larger than the balance to succeed.",
    "category": "correctness",
    "severity": "high",
    "confidence": "high",
    "file_path": "src/billing.py",
    "start_line": 14,
    "end_line": 14,
    "evidence": [
        {
            "file_path": "src/billing.py",
            "start_line": 14,
            "end_line": 14,
            "quoted_text": "return amount >= balance",
        }
    ],
    "reasoning_summary": "The comparison operands are reversed.",
    "suggested_fix": "return balance >= amount",
}

_ACCEPT_VERDICT = {
    "decision": "accept",
    "reasoning_summary": "Confirmed: the comparison is genuinely backwards.",
    "downgraded_severity": None,
    "downgraded_confidence": None,
}

_NO_FINDINGS: dict[str, list[object]] = {"findings": []}


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


def _response_factory(request: ProviderRequest) -> ScriptedResponse:
    """Route by which candidate's prompt (and, since Agent Orchestration
    v1, which specialist role -- see ``request.schema_name``) this is, so
    scripted responses are correct regardless of concurrent scheduling
    order. The fixture's bug is a correctness defect, so only the
    Correctness role's call is scripted to find it -- the Security role
    always returns no findings, keeping this test's proposal/accepted
    counts meaningful without asserting on cross-role dedup behavior
    (covered separately in ``tests/unit/test_review_agents_cross_role.py``)."""

    if request.schema_name == "critic_verdict":
        return ScriptedResponse(raw_json=json.dumps(_ACCEPT_VERDICT))
    if request.schema_name == "review_response:security":
        return ScriptedResponse(raw_json=json.dumps(_NO_FINDINGS))
    # Match on the review-target anchor line, not a bare substring -- the
    # target's own context bundle may legitimately include a sibling
    # symbol's source, so a bare substring check could misfire.
    if "Review target: `can_withdraw`" in request.user_prompt:
        return ScriptedResponse(raw_json=json.dumps({"findings": [_CAN_WITHDRAW_FINDING]}))
    return ScriptedResponse(raw_json=json.dumps(_NO_FINDINGS))


async def _setup(
    session_factory: async_sessionmaker[AsyncSession], *, full_name: str
) -> tuple[uuid.UUID, str, Path]:
    async with session_factory() as session:
        row = await RepositoryRepository().upsert(
            session, github_repository_id=abs(hash(full_name)) % (2**62),
            owner=full_name.split("/")[0], name=full_name.split("/")[-1],
            full_name=full_name, installation_id=0,
        )
        await session.commit()
        repository_id = row.id

    root = Path("/tmp") / f"pf-ai-review-e2e-{uuid.uuid4().hex[:8]}"
    snapshot = materialize_fixture_repo(root, "ai_review_python", full_name=full_name)
    await RepositoryIndexingService(session_factory=session_factory).index_local_repository(
        repository_id=repository_id, root_path=snapshot.root_path, repository_full_name=full_name
    )
    return repository_id, snapshot.commit_sha, snapshot.root_path


async def test_end_to_end_review_produces_one_accepted_finding(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    repository_id, commit_sha, root_path = await _setup(session_factory, full_name="test/review-e2e-1")
    diff_files = [_diff_marking_lines("src/billing.py", [14, 64])]  # can_withdraw bug + clean function

    reviewer = FakeLLMProvider(response_factory=_response_factory)
    critic = FakeLLMProvider(response_factory=_response_factory)
    service = PullRequestReviewService(
        session_factory=session_factory, reviewer_provider=reviewer, critic_provider=critic
    )

    # max_concurrent_requests=1: this test has >1 candidate, and SQLite's
    # single shared test connection (StaticPool) cannot interleave truly
    # concurrent transactions the way Postgres can -- real concurrent
    # review is covered against real Postgres in
    # test_review_run_concurrency.py.
    summary = await service.review_local(
        repository_id=repository_id, root_path=root_path, repository_full_name="test/review-e2e-1",
        commit_sha=commit_sha, diff_files=diff_files, config=ReviewConfig(max_concurrent_requests=1),
    )

    assert summary.status == ReviewRunStatus.SUCCEEDED
    assert summary.candidate_count == 2
    assert summary.candidates_reviewed == 2
    assert summary.candidates_failed == 0
    assert summary.accepted_count == 1
    assert summary.reviewer_usage.input_tokens > 0

    async with session_factory() as session:
        run_id = (
            (await session.execute(select(ReviewRunModel.id).where(ReviewRunModel.repository_id == repository_id)))
            .scalars()
            .one()
        )
        candidates = await ReviewCandidateRepository().list_for_run(session, review_run_id=run_id)

    assert len(candidates) == 2
    assert all(c.status == ReviewCandidateStatus.REVIEWED for c in candidates)


async def test_end_to_end_audit_trail_records_every_proposal(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    repository_id, commit_sha, root_path = await _setup(session_factory, full_name="test/review-e2e-2")
    diff_files = [_diff_marking_lines("src/billing.py", [14])]

    reviewer = FakeLLMProvider(response_factory=_response_factory)
    critic = FakeLLMProvider(response_factory=_response_factory)
    service = PullRequestReviewService(
        session_factory=session_factory, reviewer_provider=reviewer, critic_provider=critic
    )
    await service.review_local(
        repository_id=repository_id, root_path=root_path, repository_full_name="test/review-e2e-2",
        commit_sha=commit_sha, diff_files=diff_files,
    )

    async with session_factory() as session:
        run_id = (
            (await session.execute(select(ReviewRunModel.id).where(ReviewRunModel.repository_id == repository_id)))
            .scalars()
            .one()
        )
        proposals = await AIFindingProposalRepository().list_for_run(session, review_run_id=run_id)

    assert len(proposals) == 1
    assert proposals[0].status == ProposalStatus.ACCEPTED
    assert proposals[0].title == "Inverted comparison in can_withdraw"


async def test_end_to_end_no_findings_is_a_valid_outcome(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    repository_id, commit_sha, root_path = await _setup(session_factory, full_name="test/review-e2e-3")
    diff_files = [_diff_marking_lines("src/billing.py", [64])]  # clean function only

    reviewer = FakeLLMProvider(response_factory=_response_factory)
    service = PullRequestReviewService(
        session_factory=session_factory, reviewer_provider=reviewer, critic_provider=None
    )
    summary = await service.review_local(
        repository_id=repository_id, root_path=root_path, repository_full_name="test/review-e2e-3",
        commit_sha=commit_sha, diff_files=diff_files,
    )

    assert summary.status == ReviewRunStatus.SUCCEEDED
    assert summary.accepted_count == 0
    assert summary.proposals_count == 0
