"""Coverage for ``review_candidates.context_bundle_id`` -- every reviewed
candidate must be traceable to the exact persisted Phase 4 ContextBundle
used to build its provider request."""

from __future__ import annotations

import json
import uuid
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from patchfrog.context.config import ContextConfig
from patchfrog.context.domain import ContextTargetType
from patchfrog.context.service import ContextService
from patchfrog.diff.models import DiffFile, DiffHunk, DiffLine, DiffLineType
from patchfrog.indexing.service import RepositoryIndexingService
from patchfrog.persistence.models.context import ContextBundleModel
from patchfrog.persistence.models.review import (
    ReviewCandidateModel,
    ReviewCandidateStatus,
    ReviewRunModel,
)
from patchfrog.persistence.repositories import RepositoryRepository
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

    root = Path("/tmp") / f"pf-ai-review-bundle-id-{uuid.uuid4().hex[:8]}"
    snapshot = materialize_fixture_repo(root, "ai_review_python", full_name=full_name)
    await RepositoryIndexingService(session_factory=session_factory).index_local_repository(
        repository_id=repository_id, root_path=snapshot.root_path, repository_full_name=full_name
    )
    return repository_id, snapshot.commit_sha, snapshot.root_path


async def test_context_bundle_id_populated_for_reviewed_candidates(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    repository_id, commit_sha, root_path = await _setup(session_factory, full_name="test/bundle-id-1")
    diff_files = [_diff_marking_lines("src/billing.py", [14])]

    provider = FakeLLMProvider(
        response_factory=lambda req: ScriptedResponse(raw_json=json.dumps({"findings": []}))
    )
    service = PullRequestReviewService(session_factory=session_factory, reviewer_provider=provider)
    await service.review_local(
        repository_id=repository_id, root_path=root_path, repository_full_name="test/bundle-id-1",
        commit_sha=commit_sha, diff_files=diff_files,
    )

    async with session_factory() as session:
        run_id = (
            await session.execute(select(ReviewRunModel.id).where(ReviewRunModel.repository_id == repository_id))
        ).scalars().one()
        candidates = (
            await session.execute(select(ReviewCandidateModel).where(ReviewCandidateModel.review_run_id == run_id))
        ).scalars().all()

        assert len(candidates) == 1
        candidate = candidates[0]
        assert candidate.status == ReviewCandidateStatus.REVIEWED
        assert candidate.context_bundle_id is not None

        bundle = await session.get(ContextBundleModel, candidate.context_bundle_id)
        assert bundle is not None
        assert bundle.repository_id == repository_id
        assert bundle.commit_sha == commit_sha


async def test_context_bundle_id_matches_the_candidates_own_target(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The persisted bundle must be *this* candidate's own context, not
    some other candidate's -- verified by checking the bundle's target
    symbol/file matches the candidate it's attached to."""

    repository_id, commit_sha, root_path = await _setup(session_factory, full_name="test/bundle-id-2")
    diff_files = [_diff_marking_lines("src/billing.py", [14, 22])]  # two distinct symbols

    provider = FakeLLMProvider(
        response_factory=lambda req: ScriptedResponse(raw_json=json.dumps({"findings": []}))
    )
    service = PullRequestReviewService(session_factory=session_factory, reviewer_provider=provider)
    await service.review_local(
        repository_id=repository_id, root_path=root_path, repository_full_name="test/bundle-id-2",
        commit_sha=commit_sha, diff_files=diff_files,
    )

    async with session_factory() as session:
        run_id = (
            await session.execute(select(ReviewRunModel.id).where(ReviewRunModel.repository_id == repository_id))
        ).scalars().one()
        candidates = (
            await session.execute(select(ReviewCandidateModel).where(ReviewCandidateModel.review_run_id == run_id))
        ).scalars().all()

        assert len(candidates) == 2
        bundle_ids = set()
        for candidate in candidates:
            assert candidate.context_bundle_id is not None
            bundle = await session.get(ContextBundleModel, candidate.context_bundle_id)
            assert bundle is not None
            assert bundle.target_symbol_id == candidate.symbol_id
            assert bundle.target_file_path == candidate.file_path
            assert bundle.commit_sha == commit_sha
            bundle_ids.add(bundle.id)
        # Two distinct symbols -> two distinct context bundles.
        assert len(bundle_ids) == 2


async def test_reused_context_bundle_returns_the_same_persisted_id(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    repository_id, _commit_sha, root_path = await _setup(session_factory, full_name="test/bundle-id-3")

    service = ContextService(session_factory=session_factory)
    first = await service.build_context_local(
        repository_id=repository_id, root_path=root_path, repository_full_name="test/bundle-id-3",
        target_type=ContextTargetType.LINE, file_path="src/billing.py", line=14,
        config=ContextConfig(),
    )
    second = await service.build_context_local(
        repository_id=repository_id, root_path=root_path, repository_full_name="test/bundle-id-3",
        target_type=ContextTargetType.LINE, file_path="src/billing.py", line=14,
        config=ContextConfig(),
    )

    assert first.id == second.id
    assert second.reused_existing_bundle is True
