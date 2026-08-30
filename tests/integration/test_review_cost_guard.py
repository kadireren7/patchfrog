"""Cost-guard coverage: token and candidate-count budgets must be
respected even for a PR large enough to generate many candidates, and a
budget-exhausted candidate must never reach the provider (no cost
incurred for work that was never done)."""

from __future__ import annotations

import json
import uuid
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from patchfrog.diff.models import DiffFile, DiffHunk, DiffLine, DiffLineType
from patchfrog.indexing.service import RepositoryIndexingService
from patchfrog.persistence.repositories import RepositoryRepository
from patchfrog.review.config import ReviewConfig
from patchfrog.review.providers.fake import FakeLLMProvider, ScriptedResponse
from patchfrog.review.service import PullRequestReviewService
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


def _synthetic_module_diff(count: int) -> DiffFile:
    """``count`` module-level (no containing symbol) changed lines spread
    far enough apart that clustering (max span 60) keeps them as
    distinct candidates -- simulates a very large PR touching many
    unrelated regions."""

    lines = [1 + i * 200 for i in range(count)]
    return _diff_marking_lines("src/generated.py", lines)


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

    root = Path("/tmp") / f"pf-ai-review-cost-{uuid.uuid4().hex[:8]}"
    snapshot = materialize_fixture_repo(root, "ai_review_python", full_name=full_name)

    # A large generated file with no symbols at all -- every changed line
    # falls into a module-region candidate.
    (snapshot.root_path / "src" / "generated.py").write_text(
        "\n".join(f"VALUE_{i} = {i}" for i in range(1, 20_001)) + "\n"
    )
    from tests.support.git_repo import commit_all

    commit_sha = commit_all(snapshot.root_path, "add generated file")

    await RepositoryIndexingService(session_factory=session_factory).index_local_repository(
        repository_id=repository_id, root_path=snapshot.root_path, repository_full_name=full_name
    )
    return repository_id, commit_sha, snapshot.root_path


async def test_huge_pr_respects_max_candidates(session_factory: async_sessionmaker[AsyncSession]) -> None:
    repository_id, commit_sha, root_path = await _setup(session_factory, full_name="test/review-cost-1")
    diff_files = [_synthetic_module_diff(150)]

    provider = FakeLLMProvider(
        response_factory=lambda req: ScriptedResponse(raw_json=json.dumps({"findings": []}))
    )
    # max_concurrent_requests=1: many candidates against SQLite's single
    # shared test connection (StaticPool) risks the same interleaved-
    # transaction hazard noted below; real concurrency is covered against
    # Postgres in test_review_run_concurrency.py.
    config = ReviewConfig(max_candidates=20, max_concurrent_requests=1)
    service = PullRequestReviewService(session_factory=session_factory, reviewer_provider=provider)

    summary = await service.review_local(
        repository_id=repository_id, root_path=root_path, repository_full_name="test/review-cost-1",
        commit_sha=commit_sha, diff_files=diff_files, config=config,
    )

    assert summary.candidate_count <= 20
    # Agent Orchestration v1: two specialist roles (Correctness, Security)
    # call the reviewer provider per candidate by default -- still a
    # bounded multiple of max_candidates, never unbounded.
    assert len(provider.calls) <= 20 * 2


async def test_tight_total_token_budget_skips_candidates_without_calling_the_provider(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    repository_id, commit_sha, root_path = await _setup(session_factory, full_name="test/review-cost-2")
    diff_files = [_synthetic_module_diff(10)]

    provider = FakeLLMProvider(
        response_factory=lambda req: ScriptedResponse(raw_json=json.dumps({"findings": []}))
    )
    # Small enough that at most a couple of candidates can afford a call.
    # max_concurrent_requests=1: SQLite's single shared test connection
    # (StaticPool) cannot interleave truly concurrent transactions the
    # way Postgres can -- see test_review_run_concurrency.py for the
    # real-Postgres concurrent-review coverage this test intentionally
    # avoids needing.
    config = ReviewConfig(max_candidates=10, max_total_input_tokens=500, max_concurrent_requests=1)
    service = PullRequestReviewService(session_factory=session_factory, reviewer_provider=provider)

    summary = await service.review_local(
        repository_id=repository_id, root_path=root_path, repository_full_name="test/review-cost-2",
        commit_sha=commit_sha, diff_files=diff_files, config=config,
    )

    assert summary.candidates_skipped_budget > 0
    # Agent Orchestration v1: every selected role's combined estimated
    # input is reserved together per candidate (see
    # patchfrog.review.orchestration.AgentOrchestrator) -- a candidate is
    # either fully reviewed by every role its effort tier selected, or
    # fully skipped, never partial. Quality + Cost Guard (Milestone F):
    # a LIGHT-tier candidate with no real security signal only selects
    # Correctness, so the per-candidate call count is now 1 or 2 (never
    # more), not a fixed 2 -- these synthetic module-region candidates
    # have no static findings/naming signal, so they are all LIGHT.
    assert summary.candidates_reviewed <= len(provider.calls) <= summary.candidates_reviewed * 2
    assert summary.candidate_count == summary.candidates_reviewed + summary.candidates_skipped_budget
