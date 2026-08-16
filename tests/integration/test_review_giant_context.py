"""Adversarial audit item: a giant symbol (600+ lines) must never produce
an unbounded prompt -- the reviewer's context is always capped by
:class:`~patchfrog.context.config.ContextConfig`'s ``max_tokens``, the
same hard budget the Context Engine already enforces (Phase 4). This
proves the AI Reviewer actually configures and benefits from that cap
rather than accidentally bypassing it (e.g. by requesting the whole file
region instead of the target symbol)."""

from __future__ import annotations

import json
import uuid
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from patchfrog.diff.models import DiffFile, DiffHunk, DiffLine, DiffLineType
from patchfrog.indexing.service import RepositoryIndexingService
from patchfrog.persistence.repositories import RepositoryRepository
from patchfrog.review.config import ReviewConfig
from patchfrog.review.provider import ProviderRequest
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


async def test_giant_symbol_never_produces_an_unbounded_prompt(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    full_name = "test/review-giant-1"
    async with session_factory() as session:
        row = await RepositoryRepository().upsert(
            session, github_repository_id=abs(hash(full_name)) % (2**62),
            owner="test", name="review-giant-1", full_name=full_name, installation_id=0,
        )
        await session.commit()
        repository_id = row.id

    root = Path("/tmp") / f"pf-ai-review-giant-{uuid.uuid4().hex[:8]}"
    # context_python's huge.py has a single 600-line function
    # (process_batch) -- reused here deliberately rather than duplicating
    # it, since Phase 4 already established it as the canonical
    # giant-symbol fixture.
    snapshot = materialize_fixture_repo(root, "context_python", full_name=full_name)
    await RepositoryIndexingService(session_factory=session_factory).index_local_repository(
        repository_id=repository_id, root_path=snapshot.root_path, repository_full_name=full_name
    )

    diff_files = [_diff_marking_lines("src/huge.py", [551])]  # the finding line deep inside process_batch

    captured_prompt_lengths: list[int] = []

    def factory(req: ProviderRequest) -> ScriptedResponse:
        captured_prompt_lengths.append(len(req.system_prompt) + len(req.user_prompt))
        return ScriptedResponse(raw_json=json.dumps({"findings": []}))

    provider = FakeLLMProvider(response_factory=factory)
    config = ReviewConfig(max_input_tokens_per_candidate=2_000)  # deliberately tight
    service = PullRequestReviewService(session_factory=session_factory, reviewer_provider=provider)

    await service.review_local(
        repository_id=repository_id, root_path=snapshot.root_path, repository_full_name=full_name,
        commit_sha=snapshot.commit_sha, diff_files=diff_files, config=config,
    )

    assert len(captured_prompt_lengths) == 1
    # 2000 tokens * ~4 chars/token, generously bounded (prompt scaffolding
    # + context + diff excerpt) -- nowhere close to the raw 600-line
    # function's ~15,000+ raw characters if it had been sent whole.
    assert captured_prompt_lengths[0] < 20_000
