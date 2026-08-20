"""End-to-end regression coverage for Phase 7 incremental review memory.

This is the "simulated dogfood": a real, throwaway git repository (three
real commits, exercised via real ``git diff``/``git log`` plumbing --
never hand-rolled ``DiffFile`` objects), real repository indexing, real
:class:`~patchfrog.review_memory.service.IncrementalReviewMemoryService`
orchestration, and real persistence -- with :class:`FakeLLMProvider`
standing in for the network-bound reviewer/critic (no ANTHROPIC_API_KEY
is available in CI; see the module docstring of
:mod:`patchfrog.review_memory.service`, "Still no requirement for live
Anthropic validation if no credential exists").

Scenario (three PR commits against one fixed base):

    commit1: introduces a real bug in ``divide`` (math_ops.py). ``add``
             (same file) is clean and untouched.
    commit2: introduces a *second*, unrelated bug in a new function
             ``power`` (utils.py, a different file). ``divide``'s bug and
             ``add`` are both untouched by this commit.
    commit3: fixes ``divide``'s bug. ``power``'s bug is untouched.

Proves, against real persisted state:
  - ``add`` (clean, no memory finding ever attached) is never sent to the
    reviewer again after commit1 -- the original incremental savings
    mechanism, verified via ``reviewer.calls``, not just candidate counts.
  - ``divide``'s bug survives commit2 as CARRIED_FORWARD with **zero**
    reviewer/critic calls: symbol continuity UNCHANGED and its stored
    evidence snippet independently reconfirmed verbatim against the
    exact current commit's real file content
    (:mod:`patchfrog.review_memory.evidence`) -- and is suppressed from
    that commit's publication plan (Phase 6 idempotency untouched --
    there simply is no new finding row to publish in the first place).
  - ``power``'s new bug is accepted as a new memory finding in commit2,
    then carried forward again with zero provider calls in commit3 the
    same way.
  - ``divide``'s bug, once its body actually changes (commit3, fixed),
    always gets a real AI recheck regardless of evidence revalidation --
    and is RESOLVED (no re-publish, no "resolved" comment spam) once
    that recheck confirms it's gone.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from patchfrog.indexing.service import RepositoryIndexingService
from patchfrog.persistence.repositories import (
    PullRequestRepository,
    RepositoryIndexRepository,
    RepositoryRepository,
)
from patchfrog.persistence.repositories.review_memory_finding import ReviewMemoryFindingRepository
from patchfrog.repository.git import run_git
from patchfrog.review.config import ReviewConfig
from patchfrog.review.local_diff import diff_against_base
from patchfrog.review.provider import ProviderRequest
from patchfrog.review.providers.fake import FakeLLMProvider, ScriptedResponse
from patchfrog.review.service import PullRequestReviewService
from patchfrog.review_memory.config import IncrementalConfig
from patchfrog.review_memory.domain import FindingMemoryStatus, IncrementalRunMode
from patchfrog.review_memory.service import IncrementalReviewMemoryService
from tests.support.git_repo import commit_all, init_git_repo

_DIVIDE_BUGGY = '''def divide(a, b):
    return a - b  # BUG: should be a / b
'''
_DIVIDE_FIXED = '''def divide(a, b):
    return a / b
'''
_ADD = '''def add(a, b):
    return a + b
'''
_POWER_BUGGY = '''def power(a, b):
    return a * b  # BUG: should be a ** b
'''

_DIVIDE_BUG_TITLE = "divide() subtracts instead of dividing"
_POWER_BUG_TITLE = "power() multiplies instead of exponentiating"

_DIVIDE_BUG_FINDING = {
    "title": _DIVIDE_BUG_TITLE,
    "message": "divide(a, b) returns a - b, not a / b.",
    "category": "correctness",
    "severity": "high",
    "confidence": "high",
    "file_path": "math_ops.py",
    "start_line": 1,
    "end_line": 2,
    "evidence": [{"file_path": "math_ops.py", "start_line": 2, "end_line": 2, "quoted_text": "return a - b"}],
    "reasoning_summary": "Function name promises division; body performs subtraction.",
    "suggested_fix": "return a / b",
}
_POWER_BUG_FINDING = {
    "title": _POWER_BUG_TITLE,
    "message": "power(a, b) returns a * b, not a ** b.",
    "category": "correctness",
    "severity": "high",
    "confidence": "high",
    "file_path": "utils.py",
    "start_line": 1,
    "end_line": 2,
    "evidence": [{"file_path": "utils.py", "start_line": 2, "end_line": 2, "quoted_text": "return a * b"}],
    "reasoning_summary": "Function name promises exponentiation; body performs multiplication.",
    "suggested_fix": "return a ** b",
}
_NO_FINDINGS: dict[str, list[object]] = {"findings": []}
_ACCEPT_VERDICT = {
    "decision": "accept", "reasoning_summary": "confirmed", "downgraded_severity": None, "downgraded_confidence": None,
}


def _make_response_factory() -> tuple[FakeLLMProvider, FakeLLMProvider]:
    def factory(request: ProviderRequest) -> ScriptedResponse:
        if request.schema_name == "critic_verdict":
            return ScriptedResponse(raw_json=json.dumps(_ACCEPT_VERDICT))
        if "Review target: `divide`" in request.user_prompt and "return a - b" in request.user_prompt:
            return ScriptedResponse(raw_json=json.dumps({"findings": [_DIVIDE_BUG_FINDING]}))
        if "Review target: `power`" in request.user_prompt:
            return ScriptedResponse(raw_json=json.dumps({"findings": [_POWER_BUG_FINDING]}))
        return ScriptedResponse(raw_json=json.dumps(_NO_FINDINGS))

    reviewer = FakeLLMProvider(response_factory=factory)
    critic = FakeLLMProvider(response_factory=factory)
    return reviewer, critic


def _prompted_targets(provider: FakeLLMProvider) -> set[str]:
    """Every distinct candidate this provider was actually asked to
    review, extracted from its recorded calls -- the ground truth for
    "was this candidate genuinely skipped" (never inferred from counts
    alone)."""

    targets: set[str] = set()
    for call in provider.calls:
        for line in call.user_prompt.splitlines():
            if line.startswith("Review target: `"):
                targets.add(line.split("`")[1])
    return targets


async def _setup_repo(tmp_path: Path) -> Path:
    """The PR's base commit -- deliberately contains neither ``add`` nor
    ``divide`` (added together in commit1 below), so both remain part of
    the cumulative base..HEAD diff Phase 5's candidate generator sees at
    every later commit (a realistic "PR adds a new file, fixes a bug in
    a later commit of the same PR" shape) -- this is what lets ``add``'s
    genuine incremental skip be tested for real, rather than ``add``
    never being a candidate at all because it predates the PR."""

    root = tmp_path / "repo"
    root.mkdir()
    (root / "README.md").write_text("scratch repo for Phase 7 incremental review memory testing\n")
    init_git_repo(root)
    commit_all(root, "base")
    return root


async def test_incremental_review_memory_lifecycle(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    full_name = f"test/incremental-e2e-{uuid.uuid4().hex[:8]}"
    root = await _setup_repo(tmp_path)
    base_sha = _rev_parse(root, "HEAD")

    async with session_factory() as session:
        repo_row = await RepositoryRepository().upsert(
            session, github_repository_id=abs(hash(full_name)) % (2**62),
            owner=full_name.split("/")[0], name=full_name.split("/")[-1],
            full_name=full_name, installation_id=0,
        )
        await session.commit()
        repository_id = repo_row.id

        pr_row = await PullRequestRepository().upsert(
            session, repository_id=repository_id, github_pr_number=1, title="incremental e2e",
            author="test", base_sha=base_sha, head_sha=base_sha, state="open",
        )
        await session.commit()
        pull_request_id = pr_row.id

    indexing = RepositoryIndexingService(session_factory=session_factory)
    memory = IncrementalReviewMemoryService(session_factory=session_factory)
    incremental_config = IncrementalConfig()

    # ---- commit1: introduce divide's bug ----
    (root / "math_ops.py").write_text(_ADD + "\n\n" + _DIVIDE_BUGGY)
    commit_all(root, "introduce divide bug")
    commit1_sha = _rev_parse(root, "HEAD")
    await indexing.index_local_repository(repository_id=repository_id, root_path=root, repository_full_name=full_name)
    diff_files = diff_against_base(root, base_sha)

    reviewer1, critic1 = _make_response_factory()
    async with session_factory() as session:
        index1 = await RepositoryIndexRepository().get_active(session, repository_id=repository_id)
        assert index1 is not None
    full_candidates1 = await memory.build_candidates(
        repository_id=repository_id, repository_index_id=index1.id, commit_sha=commit1_sha,
        diff_files=diff_files, max_candidates=40,
    )
    prepared1 = await memory.prepare(
        pull_request_id=pull_request_id, repository_index_id=index1.id, commit_sha=commit1_sha,
        clone_url=str(root), token=None, current_candidates=full_candidates1,
        reviewer_provider="fake", reviewer_model="fake-model", incremental_config=incremental_config,
    )
    assert prepared1.plan.mode is IncrementalRunMode.FULL  # no previous generation yet
    assert not prepared1.plan.selection.ancestry_verified

    service = PullRequestReviewService(session_factory=session_factory, reviewer_provider=reviewer1, critic_provider=critic1)
    summary1 = await service.review_local(
        repository_id=repository_id, root_path=root, repository_full_name=full_name, commit_sha=commit1_sha,
        diff_files=diff_files, pull_request_id=pull_request_id,
        candidate_filter=prepared1.candidate_filter, incremental_context_fingerprint=prepared1.incremental_context_fingerprint,
        config=ReviewConfig(max_concurrent_requests=1),
    )
    assert summary1.accepted_count == 1
    reconciliation1 = await memory.finalize(
        review_run_id=summary1.run_id, repository_id=repository_id, pull_request_id=pull_request_id,
        commit_sha=commit1_sha, prepared=prepared1,
    )
    assert len(reconciliation1.new_finding_ids) == 1
    # commit1 is a FULL review (no previous memory yet) -- add is reviewed once here.
    assert "add" in _prompted_targets(reviewer1)

    # ---- commit2: unrelated bug (power) added; divide's bug untouched ----
    (root / "utils.py").write_text(_POWER_BUGGY)
    commit_all(root, "add power (buggy), unrelated to divide")
    commit2_sha = _rev_parse(root, "HEAD")
    await indexing.index_local_repository(repository_id=repository_id, root_path=root, repository_full_name=full_name)
    diff_files2 = diff_against_base(root, base_sha)

    reviewer2, critic2 = _make_response_factory()
    async with session_factory() as session:
        index2 = await RepositoryIndexRepository().get_active(session, repository_id=repository_id)
        assert index2 is not None
    full_candidates2 = await memory.build_candidates(
        repository_id=repository_id, repository_index_id=index2.id, commit_sha=commit2_sha,
        diff_files=diff_files2, max_candidates=40,
    )
    prepared2 = await memory.prepare(
        pull_request_id=pull_request_id, repository_index_id=index2.id, commit_sha=commit2_sha,
        clone_url=str(root), token=None, current_candidates=full_candidates2,
        reviewer_provider="fake", reviewer_model="fake-model", incremental_config=incremental_config,
    )
    assert prepared2.plan.mode is IncrementalRunMode.INCREMENTAL
    assert prepared2.plan.selection.ancestry_verified
    assert prepared2.plan.selection.usable_for_candidate_skipping
    # add: untouched, no memory finding attached -> genuinely skipped.
    assert full_candidates2  # sanity: add is present in the full candidate set
    add_candidate_ids = {c.fingerprint() for c in full_candidates2 if c.symbol_name == "add"}
    selected_ids = {c.fingerprint() for c in prepared2.plan.selected_candidates}
    assert add_candidate_ids and add_candidate_ids.isdisjoint(selected_ids)

    service2 = PullRequestReviewService(session_factory=session_factory, reviewer_provider=reviewer2, critic_provider=critic2)
    summary2 = await service2.review_local(
        repository_id=repository_id, root_path=root, repository_full_name=full_name, commit_sha=commit2_sha,
        diff_files=diff_files2, pull_request_id=pull_request_id,
        candidate_filter=prepared2.candidate_filter, incremental_context_fingerprint=prepared2.incremental_context_fingerprint,
        config=ReviewConfig(max_concurrent_requests=1),
    )
    reconciliation2 = await memory.finalize(
        review_run_id=summary2.run_id, repository_id=repository_id, pull_request_id=pull_request_id,
        commit_sha=commit2_sha, prepared=prepared2,
    )
    assert "add" not in _prompted_targets(reviewer2)  # never re-reviewed: real incremental savings
    # divide's finding survives untouched: symbol continuity UNCHANGED
    # *and* its stored evidence ("return a - b") is still present
    # verbatim in math_ops.py at commit2 -- zero-AI-call carry-forward,
    # not a recheck. Proven directly against the fake provider's
    # recorded calls, never inferred from status alone.
    assert "divide" not in _prompted_targets(reviewer2)
    assert "divide" not in _prompted_targets(critic2)
    divide_candidate_ids = {c.fingerprint() for c in full_candidates2 if c.symbol_name == "divide"}
    assert divide_candidate_ids and divide_candidate_ids.isdisjoint(selected_ids)
    assert summary2.accepted_count == 1  # power only -- divide never went to the reviewer at all
    assert len(reconciliation2.new_finding_ids) == 1  # power's new bug

    assert prepared2.plan.metrics.candidates_skipped_evidence_confirmed == 1  # divide
    assert prepared2.plan.metrics.candidates_skipped_finding_free == 2  # add + the math_ops.py module region
    assert prepared2.plan.metrics.provider_calls_avoided == 3

    async with session_factory() as session:
        open_findings2 = await ReviewMemoryFindingRepository().get_open_for_pr(session, pull_request_id=pull_request_id)
    statuses2 = {f.title: f.status for f in open_findings2}
    assert statuses2[_DIVIDE_BUG_TITLE] is FindingMemoryStatus.CARRIED_FORWARD
    assert statuses2[_POWER_BUG_TITLE] is FindingMemoryStatus.OPEN

    # divide's carried-forward finding must be suppressed from commit2's
    # own publication plan -- Phase 6 idempotency is untouched (the
    # underlying AIFindingModel row is real and inline-eligible), only
    # publication is suppressed.
    async with session_factory() as session:
        already_reported2 = await ReviewMemoryFindingRepository().list_carried_forward_current_finding_ids(
            session, review_run_id=summary2.run_id
        )
    assert len(already_reported2) == 1

    # ---- commit3: fix divide; power's bug untouched ----
    (root / "math_ops.py").write_text(_ADD + "\n\n" + _DIVIDE_FIXED)
    commit_all(root, "fix divide")
    commit3_sha = _rev_parse(root, "HEAD")
    await indexing.index_local_repository(repository_id=repository_id, root_path=root, repository_full_name=full_name)
    diff_files3 = diff_against_base(root, base_sha)

    reviewer3, critic3 = _make_response_factory()
    async with session_factory() as session:
        index3 = await RepositoryIndexRepository().get_active(session, repository_id=repository_id)
        assert index3 is not None
    full_candidates3 = await memory.build_candidates(
        repository_id=repository_id, repository_index_id=index3.id, commit_sha=commit3_sha,
        diff_files=diff_files3, max_candidates=40,
    )
    prepared3 = await memory.prepare(
        pull_request_id=pull_request_id, repository_index_id=index3.id, commit_sha=commit3_sha,
        clone_url=str(root), token=None, current_candidates=full_candidates3,
        reviewer_provider="fake", reviewer_model="fake-model", incremental_config=incremental_config,
    )
    service3 = PullRequestReviewService(session_factory=session_factory, reviewer_provider=reviewer3, critic_provider=critic3)
    summary3 = await service3.review_local(
        repository_id=repository_id, root_path=root, repository_full_name=full_name, commit_sha=commit3_sha,
        diff_files=diff_files3, pull_request_id=pull_request_id,
        candidate_filter=prepared3.candidate_filter, incremental_context_fingerprint=prepared3.incremental_context_fingerprint,
        config=ReviewConfig(max_concurrent_requests=1),
    )
    reconciliation3 = await memory.finalize(
        review_run_id=summary3.run_id, repository_id=repository_id, pull_request_id=pull_request_id,
        commit_sha=commit3_sha, prepared=prepared3,
    )
    assert "add" not in _prompted_targets(reviewer3)
    # power: untouched since commit2, evidence still present -> zero-AI
    # carry-forward again. divide: body actually changed (MODIFIED
    # continuity) -> evidence revalidation is never even consulted,
    # always a real recheck regardless.
    assert "power" not in _prompted_targets(reviewer3)
    assert "divide" in _prompted_targets(reviewer3)
    assert prepared3.plan.metrics.candidates_skipped_evidence_confirmed == 1  # power
    assert not reconciliation3.new_finding_ids  # nothing new
    assert len(reconciliation3.resolved_memory_finding_ids) == 1  # divide's bug resolved

    async with session_factory() as session:
        all_findings3 = await ReviewMemoryFindingRepository().get_open_for_pr(session, pull_request_id=pull_request_id)
    statuses3 = {f.title: f.status for f in all_findings3}
    assert _DIVIDE_BUG_TITLE not in statuses3  # resolved -> no longer "open"
    assert statuses3[_POWER_BUG_TITLE] is FindingMemoryStatus.CARRIED_FORWARD

    async with session_factory() as session:
        already_reported3 = await ReviewMemoryFindingRepository().list_carried_forward_current_finding_ids(
            session, review_run_id=summary3.run_id
        )
    assert len(already_reported3) == 1  # power carried forward again, suppressed again


def _rev_parse(root: Path, ref: str) -> str:
    return run_git(["-C", str(root), "rev-parse", ref]).strip()
