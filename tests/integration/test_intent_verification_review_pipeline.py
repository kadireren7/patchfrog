"""End-to-end proof that Intent Verification is wired into the real
review pipeline (:meth:`PullRequestReviewService.review_local`), not
just callable in isolation (see
``tests/integration/test_intent_verification_corpus.py`` for the
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
from retry_worker import schedule_retry


def process_payment(request):
    schedule_retry(request)
    return save(request)
'''

_CALLER = '''from service import process_payment


def handle_webhook(request):
    return process_payment(request)
'''

_RETRY_WORKER = '''def schedule_retry(request):
    return True
'''

_REPOSITORY = '''def save(request):
    return {"ok": True}
'''


async def test_intent_verification_is_computed_and_persisted_through_review_local(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    """Uses a *callee* relationship deliberately (``process_payment``
    calls ``schedule_retry``) -- see
    ``test_intent_verification_corpus.py::test_case_one_real_affected_path_forgotten``
    for why a caller relationship would be redundant with J's own
    ``CALLER_NOT_UPDATED`` companion after the correction round's dedup
    fix."""

    full_name = "test/iv-pipeline"
    root = tmp_path / "repo"
    root.mkdir()
    (root / "service.py").write_text(_SERVICE)
    (root / "caller.py").write_text(_CALLER)
    (root / "retry_worker.py").write_text(_RETRY_WORKER)
    (root / "repository.py").write_text(_REPOSITORY)
    init_git_repo(root)
    base_sha = commit_all(root, "base")

    (root / "repository.py").write_text('def save(request):\n    return {"ok": True, "idempotent": True}\n')
    (root / "service.py").write_text(
        'from repository import save\nfrom retry_worker import schedule_retry\n\n\n'
        'def process_payment(request):\n    if request.get("id") in _seen:\n        return None\n'
        '    schedule_retry(request)\n    return save(request)\n\n\n_seen = set()\n'
    )
    head_sha = commit_all(root, "prevent duplicate retry payment processing")

    async with session_factory() as session:
        repo = await RepositoryRepository().upsert(
            session, github_repository_id=abs(hash(full_name)) % (2**62),
            owner="test", name="iv-pipeline", full_name=full_name, installation_id=0,
        )
        await session.commit()
        repository_id = repo.id

    await RepositoryIndexingService(session_factory=session_factory).index_local_repository(
        repository_id=repository_id, root_path=root, repository_full_name=full_name
    )
    diff_files = diff_against_base(root, base_sha)

    provider = FakeLLMProvider(
        response_factory=lambda req: ScriptedResponse(raw_json=json.dumps({"findings": []}))
    )
    service = PullRequestReviewService(session_factory=session_factory, reviewer_provider=provider)
    await service.review_local(
        repository_id=repository_id, root_path=root, repository_full_name=full_name,
        commit_sha=head_sha, diff_files=diff_files, base_sha=base_sha,
        title="Prevent duplicate retry payment processing", body=None,
    )

    async with session_factory() as session:
        run = (
            await session.execute(select(ReviewRunModel).where(ReviewRunModel.repository_id == repository_id))
        ).scalars().one()

    assert run.intent_claim_count == 1
    assert run.mapped_intent_claim_count == 1
    assert run.intent_gap_candidate_count >= 1
    assert "Intent:" in (run.change_story or "")


async def test_review_local_without_title_body_never_computes_intent_verification(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    """``title``/``body`` omitted entirely (every review before this
    milestone, and any caller that hasn't opted in) -- Intent
    Verification must be a complete no-op, never a crash, never a
    fabricated count."""

    full_name = "test/iv-pipeline-no-metadata"
    root = tmp_path / "repo"
    root.mkdir()
    (root / "service.py").write_text(_SERVICE)
    (root / "repository.py").write_text(_REPOSITORY)
    init_git_repo(root)
    base_sha = commit_all(root, "base")

    (root / "repository.py").write_text('def save(request):\n    return {"ok": True, "v": 2}\n')
    head_sha = commit_all(root, "change save")

    async with session_factory() as session:
        repo = await RepositoryRepository().upsert(
            session, github_repository_id=abs(hash(full_name)) % (2**62),
            owner="test", name="iv-pipeline-no-metadata", full_name=full_name, installation_id=0,
        )
        await session.commit()
        repository_id = repo.id

    await RepositoryIndexingService(session_factory=session_factory).index_local_repository(
        repository_id=repository_id, root_path=root, repository_full_name=full_name
    )
    diff_files = diff_against_base(root, base_sha)

    provider = FakeLLMProvider(
        response_factory=lambda req: ScriptedResponse(raw_json=json.dumps({"findings": []}))
    )
    service = PullRequestReviewService(session_factory=session_factory, reviewer_provider=provider)
    await service.review_local(
        repository_id=repository_id, root_path=root, repository_full_name=full_name,
        commit_sha=head_sha, diff_files=diff_files,
    )

    async with session_factory() as session:
        run = (
            await session.execute(select(ReviewRunModel).where(ReviewRunModel.repository_id == repository_id))
        ).scalars().one()

    assert run.intent_claim_count == 0
    assert run.intent_gap_candidate_count == 0
