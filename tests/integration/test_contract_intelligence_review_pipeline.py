"""End-to-end proof that Contract & Blast Radius Intelligence is wired
into the real review pipeline (:meth:`PullRequestReviewService.review_local`),
not just callable in isolation (see
``tests/integration/test_contract_intelligence_corpus.py`` for the
isolated-service-call corpus). Real two-commit git repo, real indexing,
real diff, a scripted :class:`FakeLLMProvider` (no live LLM)."""

from __future__ import annotations

import json
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from patchfrog.indexing.service import RepositoryIndexingService
from patchfrog.persistence.models.review import ReviewRunModel
from patchfrog.persistence.repositories import RepositoryRepository
from patchfrog.review.local_diff import diff_against_base
from patchfrog.review.providers.fake import FakeLLMProvider, ScriptedResponse
from patchfrog.review.service import PullRequestReviewService
from tests.support.git_repo import commit_all, init_git_repo

_SERVICE = '''from repository import save


def process(request):
    return save(request)
'''

_REPOSITORY = '''def save(request):
    return {"ok": True}
'''


async def test_contract_intelligence_is_computed_and_persisted_through_review_local(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    full_name = "test/ci-pipeline-contract"
    root = tmp_path / "repo"
    root.mkdir()
    (root / "service.py").write_text(_SERVICE)
    (root / "repository.py").write_text(_REPOSITORY)
    init_git_repo(root)
    base_sha = commit_all(root, "base")

    (root / "repository.py").write_text('def save(request, retries):\n    return {"ok": True, "retries": retries}\n')
    head_sha = commit_all(root, "add required retries parameter")

    async with session_factory() as session:
        repo = await RepositoryRepository().upsert(
            session, github_repository_id=abs(hash(full_name)) % (2**62),
            owner="test", name="ci-pipeline-contract", full_name=full_name, installation_id=0,
        )
        await session.commit()
        repository_id = repo.id

    await RepositoryIndexingService(session_factory=session_factory).index_local_repository(
        repository_id=repository_id, root_path=root, repository_full_name=full_name
    )
    commit_sha = head_sha
    diff_files = diff_against_base(root, base_sha)

    provider = FakeLLMProvider(
        response_factory=lambda req: ScriptedResponse(raw_json=json.dumps({"findings": []}))
    )
    service = PullRequestReviewService(session_factory=session_factory, reviewer_provider=provider)
    await service.review_local(
        repository_id=repository_id, root_path=root, repository_full_name=full_name,
        commit_sha=commit_sha, diff_files=diff_files, base_sha=base_sha,
    )

    async with session_factory() as session:
        run = (
            await session.execute(select(ReviewRunModel).where(ReviewRunModel.repository_id == repository_id))
        ).scalars().one()

    assert run.contract_delta_count == 1
    assert run.potentially_breaking_delta_count == 1
    assert run.stale_consumer_candidate_count == 1
    assert run.impacted_consumer_count >= 1
    assert "save" in (run.change_story or "")


async def test_review_local_without_base_sha_never_computes_contract_intelligence(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    """``base_sha`` omitted entirely (every review before this milestone,
    and any caller that hasn't opted in) -- Contract Intelligence must be
    a complete no-op, never a crash, never a fabricated count."""

    full_name = "test/ci-pipeline-no-base"
    root = tmp_path / "repo"
    root.mkdir()
    (root / "service.py").write_text(_SERVICE)
    (root / "repository.py").write_text(_REPOSITORY)
    init_git_repo(root)
    base_sha = commit_all(root, "base")

    (root / "repository.py").write_text('def save(request, retries):\n    return {"ok": True, "retries": retries}\n')
    head_sha = commit_all(root, "add required retries parameter")

    async with session_factory() as session:
        repo = await RepositoryRepository().upsert(
            session, github_repository_id=abs(hash(full_name)) % (2**62),
            owner="test", name="ci-pipeline-no-base", full_name=full_name, installation_id=0,
        )
        await session.commit()
        repository_id = repo.id

    await RepositoryIndexingService(session_factory=session_factory).index_local_repository(
        repository_id=repository_id, root_path=root, repository_full_name=full_name
    )
    commit_sha = head_sha
    diff_files = diff_against_base(root, base_sha)

    provider = FakeLLMProvider(
        response_factory=lambda req: ScriptedResponse(raw_json=json.dumps({"findings": []}))
    )
    service = PullRequestReviewService(session_factory=session_factory, reviewer_provider=provider)
    await service.review_local(
        repository_id=repository_id, root_path=root, repository_full_name=full_name,
        commit_sha=commit_sha, diff_files=diff_files,
    )

    async with session_factory() as session:
        run = (
            await session.execute(select(ReviewRunModel).where(ReviewRunModel.repository_id == repository_id))
        ).scalars().one()

    assert run.contract_delta_count == 0
    assert run.stale_consumer_candidate_count == 0
