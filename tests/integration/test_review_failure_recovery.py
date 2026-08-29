"""Failure-recovery semantics: a single candidate's provider failure must
never crash the whole run, a critic failure must fall back to no-critic
aggregation rather than discarding an otherwise-valid proposal, and the
run's final status must accurately reflect partial success."""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from patchfrog.diff.models import DiffFile, DiffHunk, DiffLine, DiffLineType
from patchfrog.indexing.service import RepositoryIndexingService
from patchfrog.persistence.repositories import RepositoryRepository
from patchfrog.review.config import ReviewConfig
from patchfrog.review.domain import ReviewRunStatus
from patchfrog.review.provider import ProviderFatalError, ProviderRequest
from patchfrog.review.providers.fake import FakeLLMProvider, ScriptedResponse
from patchfrog.review.service import PullRequestReviewService
from tests.support.git_repo import materialize_fixture_repo

_ACCEPT_VERDICT = json.dumps(
    {"decision": "accept", "reasoning_summary": "ok", "downgraded_severity": None, "downgraded_confidence": None}
)


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
            owner=full_name.split("/")[0], name=full_name.split("/")[-1],
            full_name=full_name, installation_id=0,
        )
        await session.commit()
        repository_id = row.id

    root = Path("/tmp") / f"pf-ai-review-fail-{uuid.uuid4().hex[:8]}"
    snapshot = materialize_fixture_repo(root, "ai_review_python", full_name=full_name)
    await RepositoryIndexingService(session_factory=session_factory).index_local_repository(
        repository_id=repository_id, root_path=snapshot.root_path, repository_full_name=full_name
    )
    return repository_id, snapshot.commit_sha, snapshot.root_path


async def test_one_candidate_provider_failure_yields_partial_status(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    repository_id, commit_sha, root_path = await _setup(session_factory, full_name="test/review-fail-1")
    # can_withdraw + apply_payment_result -- fail the second, succeed the first.
    diff_files = [_diff_marking_lines("src/billing.py", [14, 22])]

    def factory(request: ProviderRequest) -> ScriptedResponse | Exception:
        # Match on the review-target anchor line, not a bare substring --
        # the target's own context bundle may legitimately include a
        # sibling symbol's source (e.g. apply_payment_result appearing as
        # SIBLING_SYMBOL context for can_withdraw's own review), so a
        # bare substring check would misfire on the wrong candidate.
        if "Review target: `apply_payment_result`" in request.user_prompt:
            return ProviderFatalError("simulated permanent failure")
        return ScriptedResponse(raw_json=json.dumps({"findings": []}))

    provider = FakeLLMProvider(response_factory=factory)
    service = PullRequestReviewService(session_factory=session_factory, reviewer_provider=provider)

    # max_concurrent_requests=1: this test has >1 candidate, and SQLite's
    # single shared test connection (StaticPool) cannot interleave truly
    # concurrent transactions the way Postgres can -- real concurrent
    # review is covered against real Postgres in
    # test_review_run_concurrency.py.
    summary = await service.review_local(
        repository_id=repository_id, root_path=root_path, repository_full_name="test/review-fail-1",
        commit_sha=commit_sha, diff_files=diff_files, config=ReviewConfig(max_concurrent_requests=1),
    )

    assert summary.status == ReviewRunStatus.PARTIAL
    assert summary.candidates_reviewed == 1
    assert summary.candidates_failed == 1
    assert summary.candidate_count == 2


async def test_all_candidates_failing_yields_failed_status(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    repository_id, commit_sha, root_path = await _setup(session_factory, full_name="test/review-fail-2")
    diff_files = [_diff_marking_lines("src/billing.py", [14])]

    # response_factory (not a finite queue) -- Agent Orchestration v1
    # makes two concurrent specialist calls per candidate (Correctness,
    # Security), both of which must fail for "always fails" to hold.
    provider = FakeLLMProvider(response_factory=lambda req: ProviderFatalError("always fails"))
    service = PullRequestReviewService(session_factory=session_factory, reviewer_provider=provider)

    summary = await service.review_local(
        repository_id=repository_id, root_path=root_path, repository_full_name="test/review-fail-2",
        commit_sha=commit_sha, diff_files=diff_files,
    )

    assert summary.status == ReviewRunStatus.FAILED
    assert summary.candidates_reviewed == 0
    assert summary.candidates_failed == 1


def _backwards_comparison_finding() -> dict[str, object]:
    return {
        "title": "Inverted comparison",
        "message": "amount >= balance is backwards.",
        "category": "correctness",
        "severity": "high",
        "confidence": "high",
        "file_path": "src/billing.py",
        "start_line": 14,
        "end_line": 14,
        "evidence": [
            {"file_path": "src/billing.py", "start_line": 14, "end_line": 14, "quoted_text": "return amount >= balance"}
        ],
        "reasoning_summary": "backwards",
        "suggested_fix": None,
    }


async def test_critic_schema_failure_falls_back_to_no_critic_aggregation(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A critic response that fails to parse (a typed, expected failure
    mode -- ``ResponseSchemaError``) must not discard an otherwise-valid,
    already-validated proposal: it degrades to no-critic aggregation
    rather than failing the whole run."""

    repository_id, commit_sha, root_path = await _setup(session_factory, full_name="test/review-fail-3")
    diff_files = [_diff_marking_lines("src/billing.py", [14])]

    # response_factory (not a finite queue) -- both specialist roles call
    # the reviewer; both returning the identical finding is intentional:
    # cross-role dedup (patchfrog.review.agents.cross_role) deterministically
    # collapses them to one proposal *before* critique, so exactly one
    # critic call happens, matching the critic's single scripted response.
    reviewer = FakeLLMProvider(
        response_factory=lambda req: ScriptedResponse(raw_json=json.dumps({"findings": [_backwards_comparison_finding()]}))
    )
    critic = FakeLLMProvider([ScriptedResponse(raw_json="this is not valid json at all")])

    service = PullRequestReviewService(
        session_factory=session_factory, reviewer_provider=reviewer, critic_provider=critic
    )
    summary = await service.review_local(
        repository_id=repository_id, root_path=root_path, repository_full_name="test/review-fail-3",
        commit_sha=commit_sha, diff_files=diff_files,
    )

    assert summary.status == ReviewRunStatus.SUCCEEDED
    assert summary.accepted_count == 1  # survived on reviewer confidence alone, no critic ceiling applied


async def test_untyped_critic_exception_is_not_gracefully_degraded(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Graceful degradation is deliberately scoped to the three typed
    failure classes (ProviderTransientError/ProviderFatalError/
    ResponseSchemaError) -- an unexpected bug in the critic path must
    still surface loudly (fail the run) rather than being silently
    swallowed by an overly broad except clause."""

    repository_id, commit_sha, root_path = await _setup(session_factory, full_name="test/review-fail-4")
    diff_files = [_diff_marking_lines("src/billing.py", [14])]

    # See test_critic_schema_failure_falls_back_to_no_critic_aggregation
    # above for why both specialist roles returning the identical finding
    # (via response_factory) still results in exactly one critic call.
    reviewer = FakeLLMProvider(
        response_factory=lambda req: ScriptedResponse(raw_json=json.dumps({"findings": [_backwards_comparison_finding()]}))
    )
    critic = FakeLLMProvider([RuntimeError("critic backend unavailable")])

    service = PullRequestReviewService(
        session_factory=session_factory, reviewer_provider=reviewer, critic_provider=critic
    )
    with pytest.raises(RuntimeError, match="critic backend unavailable"):
        await service.review_local(
            repository_id=repository_id, root_path=root_path, repository_full_name="test/review-fail-4",
            commit_sha=commit_sha, diff_files=diff_files,
        )
