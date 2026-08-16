"""Idempotency and toolchain-identity-invalidation coverage for the AI
Reviewer, mirroring the same pattern already established for the static
analysis engine (``test_toolchain_identity.py``) and the context engine.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from patchfrog.diff.models import DiffFile, DiffHunk, DiffLine, DiffLineType
from patchfrog.indexing.service import RepositoryIndexingService
from patchfrog.persistence.models.review import ReviewRunModel
from patchfrog.persistence.repositories import RepositoryRepository
from patchfrog.review.domain import ReviewRunStatus
from patchfrog.review.providers.fake import FakeLLMProvider, ScriptedResponse
from patchfrog.review.service import PullRequestReviewService, StaleReviewIndexError
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
            owner=full_name.split("/")[0], name=full_name.split("/")[-1],
            full_name=full_name, installation_id=0,
        )
        await session.commit()
        repository_id = row.id

    root = Path("/tmp") / f"pf-ai-review-idem-{uuid.uuid4().hex[:8]}"
    snapshot = materialize_fixture_repo(root, "ai_review_python", full_name=full_name)
    await RepositoryIndexingService(session_factory=session_factory).index_local_repository(
        repository_id=repository_id, root_path=snapshot.root_path, repository_full_name=full_name
    )
    return repository_id, snapshot.commit_sha, snapshot.root_path


def _no_findings_provider(model: str = "fake-model-1") -> FakeLLMProvider:
    return FakeLLMProvider(
        response_factory=lambda req: ScriptedResponse(raw_json=json.dumps({"findings": []})),
        model_id=model,
    )


async def test_repeated_identical_review_reuses_the_run_and_never_calls_the_provider_again(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    repository_id, commit_sha, root_path = await _setup(session_factory, full_name="test/review-idem-1")
    diff_files = [_diff_marking_lines("src/billing.py", [14])]

    provider = _no_findings_provider()
    service = PullRequestReviewService(session_factory=session_factory, reviewer_provider=provider)

    first = await service.review_local(
        repository_id=repository_id, root_path=root_path, repository_full_name="test/review-idem-1",
        commit_sha=commit_sha, diff_files=diff_files,
    )
    assert first.reused_existing_run is False
    calls_after_first = len(provider.calls)
    assert calls_after_first > 0

    second = await service.review_local(
        repository_id=repository_id, root_path=root_path, repository_full_name="test/review-idem-1",
        commit_sha=commit_sha, diff_files=diff_files,
    )
    assert second.reused_existing_run is True
    assert len(provider.calls) == calls_after_first  # no double-charge on retry

    async with session_factory() as session:
        runs = (
            (await session.execute(select(ReviewRunModel).where(ReviewRunModel.repository_id == repository_id)))
            .scalars()
            .all()
        )
    succeeded = [r for r in runs if r.status == ReviewRunStatus.SUCCEEDED]
    assert len(succeeded) == 1


async def test_model_swap_invalidates_reuse_and_calls_the_provider_again(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    repository_id, commit_sha, root_path = await _setup(session_factory, full_name="test/review-idem-2")
    diff_files = [_diff_marking_lines("src/billing.py", [14])]

    provider_a = _no_findings_provider(model="fake-model-a")
    service_a = PullRequestReviewService(session_factory=session_factory, reviewer_provider=provider_a)
    first = await service_a.review_local(
        repository_id=repository_id, root_path=root_path, repository_full_name="test/review-idem-2",
        commit_sha=commit_sha, diff_files=diff_files,
    )
    assert first.reused_existing_run is False

    provider_b = _no_findings_provider(model="fake-model-b")
    service_b = PullRequestReviewService(session_factory=session_factory, reviewer_provider=provider_b)
    second = await service_b.review_local(
        repository_id=repository_id, root_path=root_path, repository_full_name="test/review-idem-2",
        commit_sha=commit_sha, diff_files=diff_files,
    )

    assert second.reused_existing_run is False  # distinct identity, not stale-reused
    assert len(provider_b.calls) > 0

    async with session_factory() as session:
        runs = (
            (await session.execute(select(ReviewRunModel).where(ReviewRunModel.repository_id == repository_id)))
            .scalars()
            .all()
        )
    succeeded = [r for r in runs if r.status == ReviewRunStatus.SUCCEEDED]
    assert len(succeeded) == 2
    assert {r.reviewer_model for r in succeeded} == {"fake-model-a", "fake-model-b"}


async def test_stale_repository_index_is_rejected(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    repository_id, _commit_sha, root_path = await _setup(session_factory, full_name="test/review-idem-3")
    provider = _no_findings_provider()
    service = PullRequestReviewService(session_factory=session_factory, reviewer_provider=provider)

    try:
        await service.review_local(
            repository_id=repository_id, root_path=root_path, repository_full_name="test/review-idem-3",
            commit_sha="0" * 40,  # deliberately not the indexed commit
            diff_files=[],
        )
        raise AssertionError("expected StaleReviewIndexError")
    except StaleReviewIndexError:
        pass
    assert len(provider.calls) == 0  # never even attempted a provider call
