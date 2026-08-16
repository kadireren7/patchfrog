"""Integration coverage for symbol-centered review candidate generation
against the real ``ai_review_python`` fixture repository."""

from __future__ import annotations

import uuid
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from patchfrog.diff.models import DiffFile, DiffHunk, DiffLine, DiffLineType
from patchfrog.indexing.service import RepositoryIndexingService
from patchfrog.persistence.repositories import RepositoryIndexRepository, RepositoryRepository
from patchfrog.review.candidates import ReviewCandidateGenerator
from patchfrog.review.domain import ReviewCandidateReason
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
) -> tuple[uuid.UUID, uuid.UUID, Path]:
    async with session_factory() as session:
        row = await RepositoryRepository().upsert(
            session, github_repository_id=abs(hash(full_name)) % (2**62),
            owner=full_name.split("/")[0], name=full_name.split("/")[-1],
            full_name=full_name, installation_id=0,
        )
        await session.commit()
        repository_id = row.id

    root = Path("/tmp") / f"pf-ai-review-{uuid.uuid4().hex[:8]}"
    snapshot = materialize_fixture_repo(root, "ai_review_python", full_name=full_name)
    summary = await RepositoryIndexingService(session_factory=session_factory).index_local_repository(
        repository_id=repository_id, root_path=snapshot.root_path, repository_full_name=full_name
    )
    del summary

    async with session_factory() as session:
        active = await RepositoryIndexRepository().get_active(session, repository_id=repository_id)
        assert active is not None
        repository_index_id = active.id

    return repository_id, repository_index_id, snapshot.root_path


async def test_candidate_generated_for_changed_symbol(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    _repo_id, repository_index_id, _root = await _setup(session_factory, full_name="test/candidate-gen-1")
    diff_files = [_diff_marking_lines("src/billing.py", [14])]  # the can_withdraw bug line

    async with session_factory() as session:
        candidates = await ReviewCandidateGenerator().generate(
            session, repository_index_id=repository_index_id, diff_files=diff_files,
            static_findings=[], max_candidates=40,
        )

    assert len(candidates) == 1
    assert candidates[0].symbol_name == "can_withdraw"
    assert candidates[0].start_line == 9
    assert candidates[0].end_line == 14
    assert candidates[0].changed_lines == (14,)
    assert candidates[0].reason == ReviewCandidateReason.CHANGED_SYMBOL


async def test_multiple_changed_symbols_produce_multiple_candidates(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    _repo_id, repository_index_id, _root = await _setup(session_factory, full_name="test/candidate-gen-2")
    diff_files = [_diff_marking_lines("src/billing.py", [14, 22, 39])]

    async with session_factory() as session:
        candidates = await ReviewCandidateGenerator().generate(
            session, repository_index_id=repository_index_id, diff_files=diff_files,
            static_findings=[], max_candidates=40,
        )

    names = {c.symbol_name for c in candidates}
    assert names == {"can_withdraw", "apply_payment_result", "load_account_config"}


async def test_unchanged_symbol_produces_no_candidate(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    _repo_id, repository_index_id, _root = await _setup(session_factory, full_name="test/candidate-gen-3")
    diff_files = [_diff_marking_lines("src/billing.py", [14])]

    async with session_factory() as session:
        candidates = await ReviewCandidateGenerator().generate(
            session, repository_index_id=repository_index_id, diff_files=diff_files,
            static_findings=[], max_candidates=40,
        )

    names = {c.symbol_name for c in candidates}
    assert "format_cents_as_dollars" not in names


async def test_no_changed_lines_produces_no_candidates(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    _repo_id, repository_index_id, _root = await _setup(session_factory, full_name="test/candidate-gen-4")

    async with session_factory() as session:
        candidates = await ReviewCandidateGenerator().generate(
            session, repository_index_id=repository_index_id, diff_files=[],
            static_findings=[], max_candidates=40,
        )

    assert candidates == ()


async def test_max_candidates_caps_the_result(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    _repo_id, repository_index_id, _root = await _setup(session_factory, full_name="test/candidate-gen-5")
    diff_files = [_diff_marking_lines("src/billing.py", [14, 22, 39, 52, 63])]

    async with session_factory() as session:
        candidates = await ReviewCandidateGenerator().generate(
            session, repository_index_id=repository_index_id, diff_files=diff_files,
            static_findings=[], max_candidates=2,
        )

    assert len(candidates) == 2
